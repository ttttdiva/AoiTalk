"""Read-only, commit-pinned downloads for an App's latest Git change.

作業ツリーは読まず、最初に確定したcommitとfirst parentのblobだけを使う。
renameは旧pathの削除と新pathの追加としてmanifestへ記録する。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from .app_git_service import AppGitError
from .app_storage import get_app_workspace_path, is_credential_app_path

_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_UNSAFE_PATH = re.compile(r'[\\:*?"<>|\x00-\x1f\x7f]')
_MAX_FILES = 20_000
_MAX_BYTES = 512 * 1024 * 1024


def _safe_path(path: str) -> bool:
    parts = path.split("/")
    return bool(path) and not _UNSAFE_PATH.search(path) and all(
        part and part not in {".", ".."} and not part.endswith((".", " "))
        and part.casefold() not in {".git", "credential", "credentials", "secrets"}
        for part in parts
    ) and not is_credential_app_path(path)


class _CommitReader:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.git = shutil.which("git")
        if not self.git:
            raise AppGitError("git が見つかりません")
        # App repositories are isolated directories, never parent repos or
        # worktree/gitfile links. A download must not initialize/repair Git.
        git_dir = workspace / ".git"
        if workspace.is_symlink() or git_dir.is_symlink() or not git_dir.is_dir():
            raise AppGitError("このAppに読み取り可能なGit履歴がありません")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({"GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0"})
        top = self.run("rev-parse", "--show-toplevel").decode("utf-8").strip()
        if Path(top).resolve() != workspace.resolve():
            raise AppGitError("AppのGit workspaceが一致しません")

    def run(self, *args: str) -> bytes:
        try:
            result = subprocess.run(
                [str(self.git), "-c", "protocol.allow=never", *args], cwd=self.workspace, env=self.env,
                capture_output=True, check=True, timeout=60,
            )
            return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            # Git stderr may contain private paths/configuration. Keep the
            # response actionable without leaking that untrusted text.
            raise AppGitError("対象コミットのGit履歴を読み込めません。履歴を確認して再試行してください") from exc

    def resolve(self, revision: str | None) -> tuple[str, str | None]:
        if revision is not None and not _OBJECT_ID.fullmatch(revision):
            raise AppGitError("保存版のGitコミットを特定できません")
        commit = self.run("rev-parse", "--verify", f"{revision or 'HEAD'}^{{commit}}").decode("ascii").strip()
        if not _OBJECT_ID.fullmatch(commit):
            raise AppGitError("Gitコミットを特定できません")
        # Read the actual commit object, not a shallow/grafted log that can
        # incorrectly turn a missing parent into an initial commit.
        header = self.run("cat-file", "commit", commit).split(b"\n\n", 1)[0]
        parents = [line[7:].decode("ascii") for line in header.splitlines() if line.startswith(b"parent ")]
        parent = parents[0] if parents else None
        if parent is not None and not _OBJECT_ID.fullmatch(parent):
            raise AppGitError("Gitの親コミットが不正です")
        return commit, parent

    def tree(self, revision: str) -> dict[str, tuple[str, str]]:
        entries: dict[str, tuple[str, str]] = {}
        for record in self.run("ls-tree", "-r", "-z", "--full-tree", revision).split(b"\0"):
            if not record:
                continue
            try:
                attributes, path_bytes = record.split(b"\t", 1)
                mode, kind, oid = attributes.decode("ascii").split(" ")
                path = path_bytes.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise AppGitError("Gitに読み取りできないパスがあります") from exc
            if not _OBJECT_ID.fullmatch(oid):
                raise AppGitError("Gitのファイル情報が不正です")
            entries[path] = (mode, oid) if kind == "blob" else (mode, "")
        if len(entries) > _MAX_FILES:
            raise AppGitError("対象コミットのファイル数がダウンロード上限を超えています")
        return entries


def build_latest_commit_archive(
    app_id: UUID,
    root_name: str,
    workspace_root: str | Path | None,
    *,
    revision: str | None = None,
    include_path: Callable[[str], bool] = lambda _path: True,
) -> tuple[Path, str]:
    """Create a ZIP with safe changed blobs and a deletion/change manifest.

    ``revision=None`` means current HEAD. Installed releases must supply their
    exact git_revision and must never silently fall back to the current HEAD.
    Empty/deletion-only commits still produce a manifest-only archive.
    """
    reader = _CommitReader(get_app_workspace_path(app_id, workspace_root=workspace_root))
    commit, parent = reader.resolve(revision)
    after = reader.tree(commit)
    before = reader.tree(parent) if parent else {}
    # Check the complete selected target tree, not only changed paths: an
    # unchanged A.txt and a newly-added a.txt still collide on Windows.
    target_names = [path.casefold() for path, (mode, _oid) in after.items()
                    if mode in {"100644", "100755"} and _safe_path(path) and include_path(path)]
    if len(target_names) != len(set(target_names)):
        raise AppGitError("大文字小文字だけが異なる重複パスがあるためZIPを作成できません")
    name_set = set(target_names)
    if any("/".join(name.split("/")[:index]) in name_set
           for name in name_set for index in range(1, len(name.split("/")))):
        raise AppGitError("ファイル名とフォルダ名が衝突するためZIPを作成できません")
    files: list[tuple[str, str, str]] = []
    deleted: list[str] = []
    used_names: set[str] = set()
    for path in sorted(after.keys() | before.keys()):
        previous, current = before.get(path), after.get(path)
        if previous == current or not _safe_path(path) or not include_path(path):
            continue
        # Never serialize symlinks or submodules, including their targets.
        if current is not None and current[0] in {"100644", "100755"}:
            key = path.casefold()
            if key in used_names:
                raise AppGitError("大文字小文字だけが異なる重複パスがあるためZIPを作成できません")
            used_names.add(key)
            files.append((path, current[1], "modified" if previous else "added"))
        elif current is None and previous is not None and previous[0] in {"100644", "100755"}:
            deleted.append(path)
    # Include deletion names in collision checks: a case-only rename cannot
    # safely be applied to Windows by deleting after extracting the ZIP.
    if used_names.intersection(path.casefold() for path in deleted):
        raise AppGitError("大文字小文字だけの名前変更は差分ZIPで扱えません。全体ダウンロードを使ってください")
    manifest_name = ".aoitalk-latest-commit.json"
    index = 1
    root_entries = {path.split("/", 1)[0].casefold() for path in after.keys() | before.keys()}
    while manifest_name.casefold() in root_entries:
        manifest_name = f".aoitalk-latest-commit-{index}.json"
        index += 1
    manifest = {
        "schema_version": 1,
        "mode": "latest_commit",
        "revision": commit,
        "parent_revision": parent,
        "comparison": "first_parent" if parent else "initial_commit",
        "files": [{"path": path, "status": status} for path, _oid, status in files],
        "deleted_paths": deleted,
        "notes": ["renameは旧pathの削除と新pathの追加で表します", "削除は自動適用しません", "未コミット・未追跡・秘密ファイル・symlink・submoduleは含みません"],
    }
    handle, filename = tempfile.mkstemp(prefix="aoitalk-app-commit-", suffix=".zip")
    os.close(handle)
    destination = Path(filename)
    try:
        total = 0
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, oid, _status in files:
                size = int(reader.run("cat-file", "-s", oid))
                total += size
                if total > _MAX_BYTES:
                    raise AppGitError("差分ZIPのサイズがダウンロード上限を超えています")
                content = reader.run("cat-file", "blob", oid)
                if len(content) != size:
                    raise AppGitError("コミット内のファイルサイズが一致しません")
                info = zipfile.ZipInfo(f"{root_name}/{path}")
                info.create_system = 3
                info.external_attr = int(after[path][0], 8) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
            archive.writestr(f"{root_name}/{manifest_name}", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination, commit
