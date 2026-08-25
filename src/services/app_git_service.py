"""App workspace-local Git service."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

from .app_storage import (
    APP_GITIGNORE_TEXT,
    AppStorageError,
    ensure_app_gitignore,
    ensure_app_workspace,
    get_app_workspace_path,
    normalize_app_relative_path,
)


logger = logging.getLogger(__name__)

# 正本は app_storage.REQUIRED_APP_IGNORE_RULES。既存の import 互換のため
# ここでは同じ内容を再公開するだけにして、二重管理にしない。
APP_GITIGNORE = APP_GITIGNORE_TEXT
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class AppGitError(RuntimeError):
    """App Git operation failure."""


class AppGitService:
    """Manage an isolated Git repository over the entire App workspace.

    The service never accepts a caller-provided cwd or repository path. Every
    operation resolves the canonical ``_apps/app_<uuid>`` workspace first.
    """

    def __init__(self, *, workspace_root: str | os.PathLike[str] | None = None) -> None:
        self.workspace_root = workspace_root
        self.git = shutil.which("git")

    @property
    def available(self) -> bool:
        return bool(self.git)

    def workspace(self, app_id: str | UUID) -> Path:
        return get_app_workspace_path(app_id, workspace_root=self.workspace_root)

    def _run(
        self,
        workspace: Path,
        *args: str,
        check: bool = True,
        timeout: int = 60,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if not self.available:
            raise AppGitError("git が見つかりません")
        try:
            return subprocess.run(
                [str(self.git), *args],
                cwd=workspace,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=check,
                timeout=timeout,
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise AppGitError(detail or "git 操作に失敗しました") from exc
        except subprocess.TimeoutExpired as exc:
            raise AppGitError("git 操作がタイムアウトしました") from exc

    def initialize(self, app_id: str | UUID) -> Path:
        """Open (or create) the App repository and repair its ignore policy.

        `.gitignore` は「Git 管理対象だが App file API / Source Bundle からは
        更新できない」書き込み保護ファイルとして扱う。ここで必須ルールの欠落を
        検査し、欠けていれば追記したうえで、既に index に入ってしまっている
        secrets / runtime data を untrack して安全側へ寄せる。
        """
        if not self.available:
            raise AppGitError("git が見つかりません")
        workspace = ensure_app_workspace(app_id, workspace_root=self.workspace_root)
        if not (workspace / ".git").exists():
            self._run(workspace, "init", "-b", "main")
        else:
            # Appの開発版は常に main を正本ブランチとして表示する。
            # 旧実装で作られた App の master だけを移行し、他の既存ブランチは触らない。
            branch = self._run(workspace, "branch", "--show-current", check=False).stdout.strip()
            if branch == "master":
                self._run(workspace, "branch", "-M", "main")
        self._run(workspace, "config", "core.autocrlf", "false")
        added = ensure_app_gitignore(workspace)
        if added:
            logger.warning(
                "App .gitignore に必須ルールを補完しました (app=%s): %s",
                app_id,
                ", ".join(added),
            )
            self._untrack_ignored(workspace)
        return workspace

    def _untrack_ignored(self, workspace: Path) -> list[str]:
        """必須ルール補完後、既に追跡されている ignore 対象を index から外す。

        作業ツリーのファイルは消さず index からのみ落とすので、次の
        checkpoint 以降 secrets / runtime data が commit されなくなる。
        """
        listed = self._run(
            workspace,
            "ls-files",
            "-z",
            "-i",
            "-c",
            "--exclude-standard",
            check=False,
        )
        if listed.returncode != 0:
            return []
        paths = [item for item in listed.stdout.split("\0") if item]
        for start in range(0, len(paths), 100):
            self._run(
                workspace,
                "rm",
                "--cached",
                "--quiet",
                "--",
                *paths[start : start + 100],
                check=False,
            )
        if paths:
            logger.warning(
                "App Git から ignore 対象を untrack しました: %s",
                ", ".join(paths[:20]),
            )
        return paths

    def status(self, app_id: str | UUID) -> dict[str, Any]:
        workspace = self.initialize(app_id)
        status = self._run(workspace, "status", "--porcelain=v1", "--branch", check=False)
        lines = [line for line in status.stdout.splitlines() if line]
        branch = None
        files: list[dict[str, str]] = []
        for line in lines:
            if line.startswith("## "):
                branch = line[3:].split("...", 1)[0]
                continue
            if len(line) >= 3:
                files.append({"code": line[:2], "path": line[3:]})
        revision = self._resolve_head(workspace)
        return {
            "available": True,
            "clean": not files,
            "dirty": bool(files),
            "branch": branch,
            "revision": revision,
            "files": files,
        }

    def checkpoint(self, app_id: str | UUID, message: str, actor: str = "app") -> str | None:
        workspace = self.initialize(app_id)
        self._run(workspace, "add", "-A")
        staged = self._run(workspace, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return self._resolve_head(workspace)
        safe_actor = str(actor or "app").strip().replace("\n", " ")[:80] or "app"
        safe_message = str(message or "App checkpoint").strip().replace("\n", " ")[:240]
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "AoiTalk App",
                "GIT_AUTHOR_EMAIL": "app@aoitalk.local",
                "GIT_COMMITTER_NAME": "AoiTalk App",
                "GIT_COMMITTER_EMAIL": "app@aoitalk.local",
            }
        )
        self._run(workspace, "commit", "-m", f"[{safe_actor}] {safe_message}", env=env)
        return self._resolve_head(workspace)

    def history(
        self,
        app_id: str | UUID,
        *,
        path: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        workspace = self.initialize(app_id)
        args = ["log", f"-{max(1, min(int(limit), 200))}", "--format=%H%x1f%aI%x1f%an%x1f%s"]
        if path:
            args.extend(["--", self._safe_path(path)])
        result = self._run(workspace, *args, check=False)
        if result.returncode != 0:
            return []
        return [
            dict(zip(("revision", "date", "author", "message"), line.split("\x1f", 3)))
            for line in result.stdout.splitlines()
            if line
        ]

    def diff(
        self,
        app_id: str | UUID,
        rev_a: str | None = None,
        rev_b: str | None = None,
        *,
        path: str | None = None,
    ) -> str:
        workspace = self.initialize(app_id)
        if rev_a and rev_b:
            args = ["diff", self.resolve_revision(app_id, rev_a), self.resolve_revision(app_id, rev_b)]
        elif rev_a:
            args = ["diff", self.resolve_revision(app_id, rev_a)]
        else:
            args = ["diff"]
        if path:
            args.extend(["--", self._safe_path(path)])
        return self._run(workspace, *args, check=False).stdout

    def restore(self, app_id: str | UUID, path: str, revision: str) -> bool:
        return self.restore_revision(app_id, revision, path=path)

    def restore_revision(
        self,
        app_id: str | UUID,
        revision: str,
        *,
        path: str | None = None,
    ) -> bool:
        """Restore tracked App files from a revision without leaving the App workspace."""
        workspace = self.initialize(app_id)
        args = [
            "restore",
            "--source",
            self.resolve_revision(app_id, revision),
            "--",
        ]
        args.append(self._safe_path(path) if path else ".")
        self._run(
            workspace,
            *args,
        )
        return True

    def reset_to_revision(self, app_id: str | UUID, revision: str) -> str:
        """Compensate a failed cross-store update by restoring Git HEAD.

        This is intentionally narrower than the user-facing restore API.  It
        resets only the canonical App workspace, removes untracked files
        created by the failed transaction, and never accepts a caller cwd.
        Callers use it only while holding the App operation lock.
        """
        workspace = self.initialize(app_id)
        target = self.resolve_revision(app_id, revision)
        self._run(workspace, "reset", "--hard", target)
        self._run(workspace, "clean", "-fd", "--")
        return target

    def resolve_revision(self, app_id: str | UUID, revision: str) -> str:
        workspace = self.initialize(app_id)
        value = str(revision or "").strip()
        if not value or value.startswith("-") or any(ch.isspace() for ch in value):
            raise AppGitError("不正な revision です")
        result = self._run(workspace, "rev-parse", "--verify", f"{value}^{{commit}}")
        return result.stdout.strip()

    def create_release_tag(self, app_id: str | UUID, version: str, revision: str | None = None) -> str:
        workspace = self.initialize(app_id)
        tag = str(version or "").strip()
        if not _TAG_RE.fullmatch(tag) or tag.startswith("-") or ".." in tag:
            raise AppGitError("不正な release tag です")
        target = self.resolve_revision(app_id, revision) if revision else self._resolve_head(workspace)
        self._run(workspace, "tag", "-a", tag, target, "-m", f"Release {tag}")
        return tag

    def _resolve_head(self, workspace: Path) -> str | None:
        result = self._run(workspace, "rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    @staticmethod
    def _safe_path(path: str) -> str:
        try:
            value = normalize_app_relative_path(path)
        except AppStorageError as exc:
            raise AppGitError(str(exc)) from exc
        return value


__all__ = ["APP_GITIGNORE", "AppGitError", "AppGitService"]
