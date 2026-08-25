"""ワークスペースの参照監査と、参照されない保存領域の後片付け。

ワークスペースは DB と同一トランザクションにできないファイルシステムを含む。
このモジュールは、破壊的な呼び出し側が必ず「DB の現在の参照を集める →
監査する → 明示的に apply する」という順序を取れるよう、パス境界と namespace
判定を一か所にまとめる。アプリ実行中のロック取得は呼び出し側で行う。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from uuid import UUID

from .app_storage import get_workspaces_root


_NAMESPACE_ID = re.compile(r"^(?P<kind>project|user)_(?P<id>[0-9a-fA-F-]{36})$")
_QUARANTINE_MARKER = ".deleting-"
_QUARANTINE_GRACE_SECONDS = 60 * 60


@dataclass(frozen=True)
class WorkspaceGcReport:
    """監査結果。各パスは ``workspace_root`` 配下の絶対 path。"""

    workspace_root: Path
    docs_orphans: tuple[Path, ...] = ()
    project_orphans: tuple[Path, ...] = ()
    user_orphans: tuple[Path, ...] = ()
    empty_dirs: tuple[Path, ...] = ()

    @property
    def orphan_count(self) -> int:
        return len(self.docs_orphans) + len(self.project_orphans) + len(self.user_orphans)

    def to_dict(self) -> dict[str, object]:
        """CLI や管理画面で扱いやすい JSON 形式へ変換する。"""
        return {
            "workspace_root": str(self.workspace_root),
            "docs_orphans": [str(path) for path in self.docs_orphans],
            "project_orphans": [str(path) for path in self.project_orphans],
            "user_orphans": [str(path) for path in self.user_orphans],
            "empty_dirs": [str(path) for path in self.empty_dirs],
            "orphan_count": self.orphan_count,
        }


def get_user_workspace_path(
    user_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the canonical personal-storage path without creating it."""
    user_uuid = _parse_uuid(user_id, "user_id")
    root = get_workspaces_root(workspace_root)
    namespace = root / "_users"
    target = namespace / f"user_{user_uuid}"
    _assert_direct_child(root, namespace, target)
    return target


def remove_user_workspace(
    user_id: str | UUID,
    *,
    workspace_root: str | os.PathLike[str] | None = None,
) -> bool:
    """Remove one user's personal storage after the DB row was committed."""
    target = get_user_workspace_path(user_id, workspace_root=workspace_root)
    if not target.exists() and not target.is_symlink():
        return False
    _remove_tree(target)
    return True


def audit_workspace(
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    attachment_references: Iterable[str | os.PathLike[str]] = (),
    project_ids: Iterable[str | UUID] = (),
    user_ids: Iterable[str | UUID] = (),
) -> WorkspaceGcReport:
    """Find files/directories which have no current DB owner.

    Only the three namespaces with an unambiguous DB ownership rule are scanned:
    ``_docs/attachments``, ``_projects/project_<uuid>``, and
    ``_users/user_<uuid>``. App sources and Project attachments are intentionally
    not merged into this rule because they have different lifecycles.
    """
    # Audit must not create a directory just because an operator mistyped the
    # path. Lifecycle cleanup uses ``get_workspaces_root`` separately when it
    # intentionally needs the canonical root to exist.
    root = resolve_workspaces_root(workspace_root)
    docs_root = root / "_docs" / "attachments"
    project_root = root / "_projects"
    user_root = root / "_users"

    referenced = _normalize_doc_references(
        root,
        docs_root,
        attachment_references,
    )
    docs_orphans = tuple(
        path
        for path in _iter_files(docs_root)
        if _resolved(path) not in referenced
        and not (
            _is_quarantine_path(path)
            and _resolved(path.with_name(path.name.split(_QUARANTINE_MARKER, 1)[0]))
            in referenced
        )
    )
    project_orphans = tuple(
        _find_orphan_namespace_dirs(project_root, "project", project_ids)
    )
    user_orphans = tuple(_find_orphan_namespace_dirs(user_root, "user", user_ids))
    # Project/User 配下の空ディレクトリは所有者が存在する限り有効な初期状態
    # になり得るため、Docs添付の一時サブディレクトリだけを掃除対象にする。
    empty_dir_candidates = _find_empty_dirs(docs_root) if docs_root.exists() else []
    empty_dirs = tuple(
        sorted(empty_dir_candidates, key=lambda path: str(path).casefold())
    )

    return WorkspaceGcReport(
        workspace_root=root,
        docs_orphans=tuple(sorted(docs_orphans, key=lambda path: str(path).casefold())),
        project_orphans=tuple(sorted(project_orphans, key=lambda path: str(path).casefold())),
        user_orphans=tuple(sorted(user_orphans, key=lambda path: str(path).casefold())),
        empty_dirs=empty_dirs,
    )


def apply_workspace_gc(report: WorkspaceGcReport) -> dict[str, int]:
    """Apply a previously generated report.

    The report is validated again so a stale or hand-built report cannot delete a
    namespace root or a path outside the configured workspace.
    """
    root = report.workspace_root.resolve()
    docs_root = (root / "_docs" / "attachments").resolve()
    project_root = (root / "_projects").resolve()
    user_root = (root / "_users").resolve()

    removed_files = 0
    removed_dirs = 0
    for path in report.docs_orphans:
        target = _validate_under(path, docs_root, direct_child=False)
        if target.exists() or target.is_symlink():
            _remove_tree(target)
            removed_files += 1
    for path, namespace_root in (
        *((path, project_root) for path in report.project_orphans),
        *((path, user_root) for path in report.user_orphans),
    ):
        target = _validate_under(path, namespace_root, direct_child=True)
        if target.exists() or target.is_symlink():
            _remove_tree(target)
            removed_dirs += 1

    # Removing an orphan file can make its parent directory empty even when it
    # was not empty at audit time. Re-scan repeatedly so multi-level empty trees
    # disappear in the same apply.
    pending_empty_dirs = set(report.empty_dirs)
    while True:
        pending_empty_dirs.update(_find_empty_dirs(docs_root))
        if not pending_empty_dirs:
            break
        removed_any = False
        for path in sorted(pending_empty_dirs, key=lambda item: len(item.parts), reverse=True):
            target = _validate_empty_dir(path, (docs_root,))
            if target.exists() and target.is_dir() and not _is_link(target):
                try:
                    target.rmdir()
                    removed_dirs += 1
                    removed_any = True
                except OSError:
                    # A file may have appeared after the audit. The next audit
                    # will report it; cleanup must not turn that race into a
                    # failed job.
                    pass
            pending_empty_dirs.discard(path)
        if not removed_any:
            break

    return {"removed_files": removed_files, "removed_dirs": removed_dirs}


def _parse_uuid(value: str | UUID, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


def resolve_workspaces_root(
    workspace_root: str | os.PathLike[str] | None,
) -> Path:
    value = workspace_root or os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    return Path(value).expanduser().resolve()


def _assert_direct_child(root: Path, namespace: Path, target: Path) -> None:
    root_resolved = root.resolve()
    namespace_resolved = namespace.resolve()
    target_resolved = target.resolve()
    try:
        namespace_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("workspace namespace escaped the workspace root") from exc
    if namespace_resolved.parent != root_resolved or target_resolved.parent != namespace_resolved:
        raise ValueError("workspace path escaped its namespace")


def _resolved(path: Path) -> Path:
    return path.resolve(strict=False)


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError:
            return True
    try:
        return bool(
            path.lstat().st_file_attributes
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    except (FileNotFoundError, AttributeError):
        return False
    except OSError:
        return True


def _iter_files(root: Path) -> Iterable[Path]:
    if not root.is_dir() or _is_link(root):
        return ()
    files: list[Path] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not _is_link(current_path / name)
        ]
        for name in filenames:
            path = current_path / name
            if not _is_link(path) and path.is_file() and not _is_recent_quarantine(path):
                files.append(path)
    return files


def _is_quarantine_path(path: Path) -> bool:
    return _QUARANTINE_MARKER in path.name


def _is_recent_quarantine(path: Path) -> bool:
    if not _is_quarantine_path(path):
        return False
    try:
        return time.time() - path.stat().st_mtime < _QUARANTINE_GRACE_SECONDS
    except OSError:
        return True


def _normalize_doc_references(
    workspace_root: Path,
    docs_root: Path,
    references: Iterable[str | os.PathLike[str]],
) -> set[Path]:
    docs_root = docs_root.resolve()
    normalized: set[Path] = set()
    for raw_value in references:
        if raw_value is None:
            continue
        raw = Path(os.fspath(raw_value))
        if raw.is_absolute():
            candidates: list[Path] = [raw]
        else:
            parts = tuple(
                part
                for part in os.fspath(raw_value).replace("\\", "/").split("/")
                if part not in {"", "."}
            )
            if parts[:2] == ("_docs", "attachments"):
                # workspace-relative ``_docs/attachments/...``
                candidates = [workspace_root.joinpath(*parts)]
            elif len(parts) >= 3 and parts[0] == workspace_root.name and parts[1:3] == (
                "_docs",
                "attachments",
            ):
                # repository-relative ``workspaces/_docs/attachments/...``
                candidates = [workspace_root.joinpath(*parts[1:])]
            else:
                # A bare path is accepted as either workspace-relative or
                # Docs-root-relative, in that order.
                candidates = [workspace_root / raw, docs_root / raw]
        for candidate in candidates:
            resolved = _resolved(candidate)
            try:
                resolved.relative_to(docs_root)
            except ValueError:
                continue
            normalized.add(resolved)
            break
    return normalized


def _find_orphan_namespace_dirs(
    namespace_root: Path,
    kind: str,
    owned_ids: Iterable[str | UUID],
) -> list[Path]:
    if not namespace_root.is_dir() or _is_link(namespace_root):
        return []
    owned: set[UUID] = set()
    for value in owned_ids:
        try:
            owned.add(_parse_uuid(value, f"{kind}_id"))
        except ValueError:
            continue

    orphaned: list[Path] = []
    for child in namespace_root.iterdir():
        if not child.is_dir() or _is_link(child):
            continue
        match = _NAMESPACE_ID.fullmatch(child.name)
        if not match or match.group("kind") != kind:
            continue
        try:
            child_id = UUID(match.group("id"))
        except ValueError:
            continue
        if child_id not in owned:
            orphaned.append(child)
    return orphaned


def _find_empty_dirs(root: Path) -> list[Path]:
    if not root.is_dir() or _is_link(root):
        return []
    empty: list[Path] = []
    for current, directories, filenames in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        if _is_link(current_path):
            continue
        directories[:] = [name for name in directories if not _is_link(current_path / name)]
        if not directories and not filenames and current_path != root:
            empty.append(current_path)
    return empty


def _validate_under(path: Path, root: Path, *, direct_child: bool) -> Path:
    target = _resolved(path)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("cleanup target escaped its namespace") from exc
    if not relative.parts or (direct_child and len(relative.parts) != 1):
        raise ValueError("cleanup target is not a removable child")
    return target


def _validate_empty_dir(path: Path, roots: tuple[Path, ...]) -> Path:
    target = _resolved(path)
    for root in roots:
        try:
            relative = target.relative_to(root)
        except ValueError:
            continue
        if relative.parts:
            return target
    raise ValueError("empty directory escaped cleanup namespaces")


def _remove_tree(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
        return
    shutil.rmtree(target, onerror=_remove_readonly)


def _remove_readonly(function, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)
