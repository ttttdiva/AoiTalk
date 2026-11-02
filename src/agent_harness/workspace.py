"""Workspace lifecycle and path-safety helpers."""

from __future__ import annotations

import logging
import hashlib
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from .config import HarnessHookSettings
from ..utils.subprocess_env import build_aoitalk_subprocess_env

logger = logging.getLogger(__name__)


class WorkspaceSafetyError(ValueError):
    """Raised when a workspace path would escape the configured root."""


@dataclass(frozen=True)
class HookResult:
    hook: str
    ok: bool
    status: int | None = None
    output: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class _WorkspaceOwnership:
    """Process-local proof that this manager created one workspace directory."""

    path: Path
    identity: tuple[int, int]
    owner_id: str | None = None
    run_id: str | None = None
    cleanup_pending: bool = False


@dataclass
class _WorkspaceClaim:
    owner_id: str
    run_id: str
    count: int = 1


def sanitize_identifier(identifier: str | None) -> str:
    value = identifier or "work-item"
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return sanitized.strip("._") or "work-item"


def _workspace_storage_name(identifier: str | None) -> str:
    """Return a stable, collision-resistant storage name for one identifier."""

    value = identifier or "work-item"
    sanitized = sanitize_identifier(identifier)
    if value == sanitized:
        return sanitized
    digest = hashlib.sha256(str(value).encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{sanitized}--{digest}"


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
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except (FileNotFoundError, OSError):
        return None
    if _is_reparse_or_symlink(path) or not stat.S_ISDIR(info.st_mode):
        return None
    return int(info.st_dev), int(info.st_ino)


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_existing_components_are_safe(path: Path) -> None:
    """Reject existing aliases without creating a missing path."""

    anchor = Path(path.anchor)
    current = anchor
    for component in path.parts[1:]:
        current = current / component
        if _is_reparse_or_symlink(current):
            raise WorkspaceSafetyError(
                f"workspace path contains a symlink/junction/reparse point: {current}"
            )


def assert_path_within_root(
    path: Path,
    root: Path,
    *,
    create: bool = False,
) -> Path:
    """Resolve a workspace path without materialising it by default.

    Path resolution is used by readers, cleanup checks, and collision checks;
    those callers must not create a workspace root as a side effect.  The
    explicit ``create=True`` compatibility option is reserved for write paths.
    """

    lexical_root = _lexical_absolute(Path(root).expanduser())
    _assert_existing_components_are_safe(lexical_root)
    if lexical_root.exists() and not lexical_root.is_dir():
        raise WorkspaceSafetyError(f"workspace root is not a directory: {lexical_root}")
    if create:
        lexical_root.mkdir(parents=True, exist_ok=True)
        _assert_existing_components_are_safe(lexical_root)

    canonical_root = lexical_root.resolve(strict=False)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = lexical_root / candidate
    candidate = _lexical_absolute(candidate)
    _assert_existing_components_are_safe(candidate)
    canonical_path = candidate.resolve(strict=False)

    if canonical_path == canonical_root:
        raise WorkspaceSafetyError(f"Workspace must not equal root: {canonical_root}")
    if canonical_root not in canonical_path.parents:
        raise WorkspaceSafetyError(
            f"Workspace path escapes root: path={canonical_path} root={canonical_root}"
        )
    return canonical_path


class WorkspaceManager:
    """Creates deterministic git worktrees for work items."""

    def __init__(
        self,
        root: Path,
        hooks: HarnessHookSettings,
        *,
        repo_root: Path | None = None,
        base_ref: str = "origin/main",
        branch_prefix: str = "harness/",
    ):
        self.root = _lexical_absolute(Path(root).expanduser())
        # The configured root is a path setting, not a write operation.  Keep
        # it absent until create_for() explicitly materialises a workspace.
        assert_path_within_root(self.root / ".workspace-probe", self.root)
        self.hooks = hooks
        self.repo_root = repo_root
        self.base_ref = base_ref
        self.branch_prefix = branch_prefix
        self._owned_workspaces: dict[Path, _WorkspaceOwnership] = {}
        self._active_claims: dict[Path, _WorkspaceClaim] = {}

    def workspace_path_for(self, identifier: str) -> Path:
        return assert_path_within_root(
            self.root / _workspace_storage_name(identifier), self.root
        )

    @staticmethod
    def _claim_values(
        owner_id: str | None,
        run_id: str | None,
    ) -> tuple[str, str] | None:
        if owner_id is None and run_id is None:
            return None
        owner = str(owner_id or "").strip()
        run = str(run_id or "").strip()
        if not owner or not run:
            raise WorkspaceSafetyError(
                "workspace owner_id and run_id are both required for an active claim"
            )
        return owner, run

    def _claim_workspace(
        self,
        workspace: Path,
        *,
        owner_id: str | None,
        run_id: str | None,
    ) -> None:
        values = self._claim_values(owner_id, run_id)
        if values is None:
            return
        owner, run = values
        current = self._active_claims.get(workspace)
        if current is None:
            self._active_claims[workspace] = _WorkspaceClaim(owner, run)
            return
        if (current.owner_id, current.run_id) != (owner, run):
            raise WorkspaceSafetyError(
                f"workspace is actively claimed by another run: {workspace}"
            )
        current.count += 1

    def release_for(
        self,
        identifier: str,
        *,
        owner_id: str,
        run_id: str,
    ) -> None:
        """Release one active run claim without deleting its workspace."""

        workspace = self.workspace_path_for(identifier)
        claim = self._active_claims.get(workspace)
        if claim is None or (claim.owner_id, claim.run_id) != (str(owner_id), str(run_id)):
            return
        if claim.count > 1:
            claim.count -= 1
        else:
            self._active_claims.pop(workspace, None)

    def create_for(
        self,
        identifier: str,
        *,
        owner_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[Path, bool]:
        workspace = self.workspace_path_for(identifier)
        if os.path.lexists(workspace):
            if _is_reparse_or_symlink(workspace):
                raise WorkspaceSafetyError(
                    f"workspace path is a symlink/junction/reparse point: {workspace}"
                )
            if not workspace.is_dir():
                raise WorkspaceSafetyError(
                    f"workspace path is not a directory: {workspace}"
                )
            ownership = self._owned_workspaces.get(workspace)
            if ownership is not None and ownership.cleanup_pending:
                raise WorkspaceSafetyError(
                    f"workspace cleanup is pending and must be retried: {workspace}"
                )
            if (
                ownership is not None
                and owner_id is not None
                and ownership.owner_id != str(owner_id)
            ):
                raise WorkspaceSafetyError(
                    f"workspace is already owned by another run: {workspace}"
                )
            self._claim_workspace(workspace, owner_id=owner_id, run_id=run_id)
            return workspace, False

        # This is the explicit write boundary.  A path getter above remains
        # read-only and does not create the configured root.
        assert_path_within_root(self.root / ".workspace-probe", self.root, create=True)
        created_identity: tuple[int, int] | None = None
        try:
            # _create_workspace returns an identity only after its own create
            # operation succeeds.  An exception carries no ownership proof:
            # another process may have won the race after the lexists check.
            created_identity = self._create_workspace(workspace, identifier)
            if created_identity is None:
                raise WorkspaceSafetyError(
                    f"created workspace is not a safe directory: {workspace}"
                )
            ownership = _WorkspaceOwnership(
                path=workspace,
                identity=created_identity,
                owner_id=str(owner_id) if owner_id is not None else None,
                run_id=str(run_id) if run_id is not None else None,
            )
            self._owned_workspaces[workspace] = ownership
            try:
                self._claim_workspace(workspace, owner_id=owner_id, run_id=run_id)
            except BaseException:
                self._owned_workspaces.pop(workspace, None)
                self._remove_owned_path(workspace, created_identity, invoke_hook=False)
                raise
            if self.hooks.after_create:
                result = run_hook(
                    "after_create",
                    self.hooks.after_create,
                    workspace,
                    timeout_ms=self.hooks.timeout_ms,
                )
                if not result.ok:
                    raise RuntimeError(f"after_create hook failed: {result.output}")
            return workspace, True
        except BaseException:
            # A failed create/after_create hook may leave a partial worktree.
            # Remove only the exact directory that was absent before this call;
            # a replacement, reparse point, or unknown owner is preserved.
            if created_identity is not None:
                self._remove_owned_path(
                    workspace,
                    created_identity,
                    invoke_hook=False,
                )
            self._active_claims.pop(workspace, None)
            self._owned_workspaces.pop(workspace, None)
            raise

    def _create_workspace(
        self,
        workspace: Path,
        identifier: str,
    ) -> tuple[int, int] | None:
        repo_root = self._repo_root()
        if repo_root is None:
            # The target was checked as absent immediately before this call.
            # ``exist_ok=False`` is essential: if another owner wins the race,
            # this call raises and the exception path must preserve their path.
            workspace.mkdir(parents=False, exist_ok=False)
            return _directory_identity(workspace)

        workspace.parent.mkdir(parents=True, exist_ok=True)
        branch_name = self._branch_name(identifier)
        add = _run_git(
            repo_root,
            [
                "worktree",
                "add",
                "-b",
                branch_name,
                str(workspace),
                self.base_ref,
            ],
        )
        if add.ok:
            return _directory_identity(workspace)

        branch_exists = _run_git(
            repo_root,
            ["rev-parse", "--verify", "--quiet", branch_name],
        ).ok
        if not branch_exists:
            raise RuntimeError(f"git worktree add failed: {add.output}")

        reuse = _run_git(repo_root, ["worktree", "add", str(workspace), branch_name])
        if not reuse.ok:
            raise RuntimeError(f"git worktree add failed: {reuse.output}")
        return _directory_identity(workspace)

    def _repo_root(self) -> Path | None:
        if self.repo_root is None:
            return None
        result = _run_git(self.repo_root, ["rev-parse", "--show-toplevel"])
        if not result.ok:
            return None
        return Path(result.output.strip()).resolve()

    def _branch_name(self, identifier: str) -> str:
        return f"{self.branch_prefix}{sanitize_identifier(identifier)}"

    def run_before_run(self, workspace: Path) -> None:
        if not self.hooks.before_run:
            return
        result = run_hook(
            "before_run",
            self.hooks.before_run,
            workspace,
            timeout_ms=self.hooks.timeout_ms,
        )
        if not result.ok:
            raise RuntimeError(f"before_run hook failed: {result.output}")

    def run_after_run(self, workspace: Path) -> HookResult | None:
        if not self.hooks.after_run:
            return None
        result = run_hook(
            "after_run",
            self.hooks.after_run,
            workspace,
            timeout_ms=self.hooks.timeout_ms,
        )
        if not result.ok:
            logger.warning("after_run hook failed: %s", _truncate(result.output))
        return result

    def run_before_remove(self, workspace: Path) -> HookResult | None:
        if (
            not self.hooks.before_remove
            or not workspace.exists()
            or _is_reparse_or_symlink(workspace)
        ):
            return None
        result = run_hook(
            "before_remove",
            self.hooks.before_remove,
            workspace,
            timeout_ms=self.hooks.timeout_ms,
        )
        if not result.ok:
            logger.warning("before_remove hook failed: %s", _truncate(result.output))
        return result

    def _remove_owned_path(
        self,
        workspace: Path,
        identity: tuple[int, int],
        *,
        invoke_hook: bool = True,
    ) -> bool:
        if _directory_identity(workspace) != identity:
            return False
        if _is_reparse_or_symlink(workspace):
            return False
        if invoke_hook:
            self.run_before_remove(workspace)
            if _directory_identity(workspace) != identity or _is_reparse_or_symlink(workspace):
                return False
        if not workspace.exists():
            return True
        repo_root = self._repo_root()
        if repo_root is not None and (workspace / ".git").exists():
            result = _run_git(repo_root, ["worktree", "remove", "--force", str(workspace)])
            if result.ok and not workspace.exists():
                return not workspace.exists()
            if not result.ok:
                logger.warning("git worktree remove failed: %s", _truncate(result.output))
        # Re-check identity after hooks/git before the destructive fallback.
        if _directory_identity(workspace) != identity or _is_reparse_or_symlink(workspace):
            return False
        shutil.rmtree(workspace)
        return True

    def remove_for(
        self,
        identifier: str,
        *,
        owner_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        workspace = self.workspace_path_for(identifier)
        ownership = self._owned_workspaces.get(workspace)
        if ownership is None:
            # The manager cannot prove that an unknown/preexisting workspace is
            # ours.  Keep it intact and, crucially, do not create its parent.
            return
        claim = self._active_claims.get(workspace)
        if claim is not None:
            if run_id is None or (claim.owner_id, claim.run_id) != (
                str(owner_id or ""),
                str(run_id),
            ):
                return
            if claim.count > 1:
                claim.count -= 1
                return
            self._active_claims.pop(workspace, None)
        if owner_id is not None and ownership.owner_id != str(owner_id):
            return
        if run_id is not None and ownership.run_id != str(run_id):
            return
        try:
            removed = self._remove_owned_path(workspace, ownership.identity)
        except Exception:
            current_identity = _directory_identity(workspace)
            if current_identity == ownership.identity and not _is_reparse_or_symlink(workspace):
                self._owned_workspaces[workspace] = replace(
                    ownership,
                    cleanup_pending=True,
                )
            else:
                self._owned_workspaces.pop(workspace, None)
            raise
        current_identity = _directory_identity(workspace)
        if removed or current_identity != ownership.identity or _is_reparse_or_symlink(workspace):
            # A collision/reparse replacement is ambiguous and must no longer
            # be associated with the old owner.
            self._owned_workspaces.pop(workspace, None)
        else:
            self._owned_workspaces[workspace] = replace(
                ownership,
                cleanup_pending=True,
            )


def run_hook(hook: str, command: str, cwd: Path, *, timeout_ms: int) -> HookResult:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=build_aoitalk_subprocess_env(extra_env={"NO_COLOR": "1"}),
            timeout=max(0.001, timeout_ms / 1000),
            check=False,
        )
        output = _truncate(completed.stdout or "")
        return HookResult(hook=hook, ok=completed.returncode == 0, status=completed.returncode, output=output)
    except subprocess.TimeoutExpired as exc:
        output = _truncate((exc.stdout or "") if isinstance(exc.stdout, str) else "")
        return HookResult(hook=hook, ok=False, output=output, timed_out=True)


def _truncate(output: str, max_chars: int = 2048) -> str:
    if len(output) <= max_chars:
        return output
    return output[:max_chars] + "... (truncated)"


def _run_git(cwd: Path, args: list[str]) -> HookResult:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
        return HookResult(
            hook="git",
            ok=completed.returncode == 0,
            status=completed.returncode,
            output=_truncate(completed.stdout or ""),
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return HookResult(hook="git", ok=False, output=_truncate(output), timed_out=True)
