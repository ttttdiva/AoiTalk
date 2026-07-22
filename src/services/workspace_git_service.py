"""Project workspace-local Git versioning."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

from .project_workspace_cleanup import get_project_workspace_path

logger = logging.getLogger(__name__)

DEFAULT_GITIGNORE = "/*\n!/.gitignore\n!/.agents/\n!/tools/\n!/docs/\n"
TRACKED_PATHS = (".gitignore", ".agents", "tools", "docs")
_warned_git_missing = False


class WorkspaceGitService:
    """Manage an isolated, local-only Git repository in a project workspace."""

    def __init__(self, *, workspace_root: str | os.PathLike[str] | None = None) -> None:
        self.workspace_root = workspace_root
        self.git = shutil.which("git")

    @property
    def available(self) -> bool:
        global _warned_git_missing
        if self.git:
            return True
        if not _warned_git_missing:
            logger.warning("git が見つからないため workspace 版管理を無効化します")
            _warned_git_missing = True
        return False

    def workspace(self, project_id: str | UUID) -> Path:
        return get_project_workspace_path(UUID(str(project_id)), workspace_root=self.workspace_root)

    def _run(self, workspace: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if not self.available:
            raise RuntimeError("git is unavailable")
        return subprocess.run(
            [str(self.git), *args], cwd=workspace, text=True, encoding="utf-8",
            errors="replace", capture_output=True, check=check, timeout=30,
        )

    def ensure_repo(self, project_id: str | UUID) -> bool:
        """Initialize versioning lazily, preserving existing repositories/files."""
        if not self.available:
            return False
        workspace = self.workspace(project_id)
        workspace.mkdir(parents=True, exist_ok=True)
        if not (workspace / ".git").exists():
            self._run(workspace, "init")
        self._run(workspace, "config", "core.autocrlf", "false")
        ignore = workspace / ".gitignore"
        if not ignore.exists():
            ignore.write_text(DEFAULT_GITIGNORE, encoding="utf-8", newline="\n")
        return True

    def checkpoint(self, project_id: str | UUID, message: str, actor: str) -> str | None:
        if not self.ensure_repo(project_id):
            return None
        workspace = self.workspace(project_id)
        for tracked_path in TRACKED_PATHS:
            # Missing untracked paths are invalid pathspecs, while missing tracked
            # paths must still be staged as deletions. Run independently and ignore
            # only the former case.
            self._run(workspace, "add", "-A", "--", tracked_path, check=False)
        if self._run(workspace, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return None
        commit_message = f"[{actor}] {message}"
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "AoiTalk Agent", "GIT_AUTHOR_EMAIL": "agent@aoitalk.local",
            "GIT_COMMITTER_NAME": "AoiTalk Agent", "GIT_COMMITTER_EMAIL": "agent@aoitalk.local",
        })
        subprocess.run(
            [str(self.git), "commit", "-m", commit_message], cwd=workspace,
            text=True, encoding="utf-8", errors="replace", capture_output=True,
            check=True, env=env, timeout=30,
        )
        return self._run(workspace, "rev-parse", "HEAD").stdout.strip()

    def history(self, project_id: str | UUID, path: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if not self.ensure_repo(project_id):
            return []
        workspace = self.workspace(project_id)
        args = ["log", f"-{max(1, min(int(limit), 200))}", "--format=%H%x1f%aI%x1f%an%x1f%s"]
        if path:
            args.extend(["--", self._safe_relative(path)])
        result = self._run(workspace, *args, check=False)
        if result.returncode != 0:
            return []
        return [dict(zip(("revision", "date", "author", "message"), line.split("\x1f", 3))) for line in result.stdout.splitlines() if line]

    def diff(self, project_id: str | UUID, rev_a: str, rev_b: str, path: str | None = None) -> str:
        if not self.ensure_repo(project_id):
            return ""
        workspace = self.workspace(project_id)
        hash_a = self._resolve_revision(workspace, rev_a)
        hash_b = self._resolve_revision(workspace, rev_b)
        args = ["diff", hash_a, hash_b]
        if path:
            args.extend(["--", self._safe_relative(path)])
        return self._run(workspace, *args).stdout

    def restore(self, project_id: str | UUID, path: str, rev: str) -> bool:
        if not self.ensure_repo(project_id):
            return False
        workspace = self.workspace(project_id)
        revision = self._resolve_revision(workspace, rev)
        self._run(workspace, "restore", "--source", revision, "--", self._safe_relative(path))
        return True

    def _resolve_revision(self, workspace: Path, revision: str) -> str:
        value = str(revision).strip()
        if not value or value.startswith("-") or any(ch.isspace() for ch in value):
            raise ValueError("不正な revision です")
        return self._run(workspace, "rev-parse", "--verify", f"{value}^{{commit}}").stdout.strip()

    @staticmethod
    def _safe_relative(path: str) -> str:
        value = str(path).replace("\\", "/").strip("/")
        parts = Path(value).parts
        if (not value or any(part in {"..", ".git"} for part in parts) or Path(value).is_absolute()
                or parts[0] not in {".gitignore", ".agents", "tools", "docs"}):
            raise ValueError("workspace 内の安全な相対パスを指定してください")
        return value


def project_id_for_workspace_path(path: Path, workspace_root: Path | None = None) -> str | None:
    """Extract a project UUID only from the canonical project workspace layout."""
    root = (workspace_root or Path(os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces"))).resolve()
    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        return None
    if len(relative.parts) < 2 or relative.parts[0] != "_projects" or not relative.parts[1].startswith("project_"):
        return None
    try:
        return str(UUID(relative.parts[1][len("project_"):]))
    except ValueError:
        return None


def checkpoint_before_destructive_path(path: Path) -> None:
    """Create an insurance checkpoint before moving/deleting tracked workspace files."""
    project_id = project_id_for_workspace_path(path)
    if not project_id:
        return
    workspace = WorkspaceGitService().workspace(project_id)
    try:
        relative = path.resolve().relative_to(workspace)
    except ValueError:
        return
    if relative.parts and relative.parts[0] in {".agents", "tools", "docs"}:
        try:
            WorkspaceGitService().checkpoint(project_id, "破壊的操作前の保険チェックポイント", "file_explorer")
        except Exception:  # noqa: BLE001
            logger.warning("破壊的操作前の workspace checkpoint に失敗しました", exc_info=True)


def tracked_workspace_fingerprint(project_id: str) -> tuple[tuple[str, int, int], ...]:
    """Return a cheap snapshot without creating or mutating the workspace."""
    workspace = WorkspaceGitService().workspace(project_id)
    rows: list[tuple[str, int, int]] = []
    for name in TRACKED_PATHS:
        target = workspace / name
        candidates = [target] if target.is_file() else (target.rglob("*") if target.is_dir() else [])
        for path in candidates:
            if path.is_file() and ".git" not in path.parts:
                stat = path.stat()
                rows.append((path.relative_to(workspace).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(rows))


def auto_checkpoint_if_changed(project_id: str | None, before: tuple[tuple[str, int, int], ...] | None) -> None:
    try:
        if not project_id or before is None or tracked_workspace_fingerprint(project_id) == before:
            return
        WorkspaceGitService().checkpoint(project_id, "エージェントターン終了時の自動チェックポイント", "agent")
    except Exception:  # noqa: BLE001
        logger.warning("workspace 自動チェックポイントに失敗しました", exc_info=True)
