"""Workspace lifecycle and path-safety helpers."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import HarnessHookSettings

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


def sanitize_identifier(identifier: str | None) -> str:
    value = identifier or "work-item"
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return sanitized.strip("._") or "work-item"


def assert_path_within_root(path: Path, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    canonical_root = root.resolve()
    candidate = path
    if not candidate.is_absolute():
        candidate = canonical_root / candidate
    parent = candidate.parent
    parent.mkdir(parents=True, exist_ok=True)
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
        self.root = root
        self.hooks = hooks
        self.repo_root = repo_root
        self.base_ref = base_ref
        self.branch_prefix = branch_prefix

    def workspace_path_for(self, identifier: str) -> Path:
        return assert_path_within_root(self.root / sanitize_identifier(identifier), self.root)

    def create_for(self, identifier: str) -> tuple[Path, bool]:
        workspace = self.workspace_path_for(identifier)
        created_now = False
        if workspace.exists() and not workspace.is_dir():
            workspace.unlink()
        if not workspace.exists():
            self._create_workspace(workspace, identifier)
            created_now = True
        if created_now and self.hooks.after_create:
            result = run_hook(
                "after_create",
                self.hooks.after_create,
                workspace,
                timeout_ms=self.hooks.timeout_ms,
            )
            if not result.ok:
                raise RuntimeError(f"after_create hook failed: {result.output}")
        return workspace, created_now

    def _create_workspace(self, workspace: Path, identifier: str) -> None:
        repo_root = self._repo_root()
        if repo_root is None:
            workspace.mkdir(parents=True, exist_ok=True)
            return

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
            return

        branch_exists = _run_git(
            repo_root,
            ["rev-parse", "--verify", "--quiet", branch_name],
        ).ok
        if not branch_exists:
            raise RuntimeError(f"git worktree add failed: {add.output}")

        reuse = _run_git(repo_root, ["worktree", "add", str(workspace), branch_name])
        if not reuse.ok:
            raise RuntimeError(f"git worktree add failed: {reuse.output}")

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
        if not self.hooks.before_remove or not workspace.exists():
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

    def remove_for(self, identifier: str) -> None:
        workspace = self.workspace_path_for(identifier)
        self.run_before_remove(workspace)
        if not workspace.exists():
            return
        repo_root = self._repo_root()
        if repo_root is not None and (workspace / ".git").exists():
            result = _run_git(repo_root, ["worktree", "remove", "--force", str(workspace)])
            if result.ok:
                return
            logger.warning("git worktree remove failed: %s", _truncate(result.output))
        shutil.rmtree(workspace)


def run_hook(hook: str, command: str, cwd: Path, *, timeout_ms: int) -> HookResult:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
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
