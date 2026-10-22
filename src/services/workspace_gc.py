"""ワークスペースの参照監査と、参照されない保存領域の後片付け。

ワークスペースは DB と同一トランザクションにできないファイルシステムを含む。
このモジュールは、破壊的な呼び出し側が必ず「DB の現在の参照を集める →
監査する → 明示的に apply する」という順序を取れるよう、パス境界と namespace
判定を一か所にまとめる。アプリ実行中のロック取得は呼び出し側で行う。
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable
from uuid import UUID, uuid4

from .app_storage import get_workspaces_root


_NAMESPACE_ID = re.compile(r"^(?P<kind>project|user)_(?P<id>[0-9a-fA-F-]{36})$")
_QUARANTINE_MARKER = ".deleting-"
_QUARANTINE_GRACE_SECONDS = 60 * 60
_RECYCLE_BIN = "_recycle_bin"
_GC_BATCHES = "workspace_orphans"


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
    """Move one user's personal storage to the recycle bin after the DB row was committed."""
    target = get_user_workspace_path(user_id, workspace_root=workspace_root)
    if not target.exists() and not target.is_symlink():
        return False
    apply_workspace_gc(
        WorkspaceGcReport(
            workspace_root=get_workspaces_root(workspace_root),
            user_orphans=(target,),
        )
    )
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


def apply_workspace_gc(report: WorkspaceGcReport) -> dict[str, int | str]:
    """Move reported orphan files/directories into a restorable recycle batch.

    Empty directories are retained. The report is revalidated so a stale or
    hand-built report cannot move namespace roots, links, or paths outside the
    configured workspace.
    """
    root = report.workspace_root.resolve()
    docs_root = (root / "_docs" / "attachments").resolve()
    project_root = (root / "_projects").resolve()
    user_root = (root / "_users").resolve()
    candidates: list[tuple[Path, str]] = []
    for path in report.docs_orphans:
        source = _validate_gc_source(path, docs_root, direct_child=False)
        if not source.is_file():
            raise ValueError("Docs orphan is no longer a regular file")
        candidates.append((source, "file"))
    for path, namespace_root in (
        *((path, project_root) for path in report.project_orphans),
        *((path, user_root) for path in report.user_orphans),
    ):
        source = _validate_gc_source(path, namespace_root, direct_child=True)
        if not source.is_dir():
            raise ValueError("orphan namespace is no longer a directory")
        candidates.append((source, "directory"))

    if not candidates:
        return {
            "quarantined_files": 0,
            "quarantined_dirs": 0,
            "batch_id": "",
            "quarantine_root": "",
        }

    batch_id = str(uuid4())
    recycle_parent = root / _RECYCLE_BIN
    batches_root = recycle_parent / _GC_BATCHES
    for directory in (recycle_parent, batches_root):
        if _is_link(directory):
            raise ValueError("workspace recycle bin cannot contain links")
        directory.mkdir(exist_ok=True)
        if _is_link(directory):
            raise ValueError("workspace recycle bin cannot contain links")
    batch_root = batches_root / batch_id
    batch_root.mkdir(exist_ok=False)
    items = [
        {
            "source": source.relative_to(root).as_posix(),
            "kind": kind,
            "status": "pending",
        }
        for source, kind in candidates
    ]
    manifest = {
        "schema": 1,
        "batch_id": batch_id,
        "status": "quarantining",
        "items": items,
    }
    _write_gc_manifest(batch_root / "manifest.json", manifest)

    moved_files = 0
    moved_dirs = 0
    for item in items:
        relative = _validated_gc_relative(item["source"])
        source = root.joinpath(*relative.parts)
        namespace_root = (
            root / "_docs" / "attachments"
            if item["kind"] == "file"
            else root / relative.parts[0]
        )
        source = _validate_gc_source(
            source,
            namespace_root,
            direct_child=item["kind"] == "directory",
        )
        destination = batch_root / "payload" / Path(*relative.parts)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("recycle-bin destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if _has_link_component(destination.parent, batch_root):
            raise ValueError("recycle-bin destination contains a link")
        item["status"] = "moving"
        _write_gc_manifest(batch_root / "manifest.json", manifest)
        source.replace(destination)
        item["status"] = "moved"
        _write_gc_manifest(batch_root / "manifest.json", manifest)
        if item["kind"] == "file":
            moved_files += 1
        else:
            moved_dirs += 1

    manifest["status"] = "quarantined"
    _write_gc_manifest(batch_root / "manifest.json", manifest)
    return {
        "quarantined_files": moved_files,
        "quarantined_dirs": moved_dirs,
        "batch_id": batch_id,
        "quarantine_root": str(batch_root),
    }


def restore_workspace_gc(
    batch_id: str,
    *,
    workspace_root: str | os.PathLike[str],
) -> dict[str, int | str]:
    """Restore a quarantine batch without overwriting newer workspace files."""
    try:
        parsed_batch_id = UUID(str(batch_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("batch_id must be a UUID") from exc
    if str(parsed_batch_id) != str(batch_id):
        raise ValueError("batch_id must be a canonical UUID")

    root = Path(workspace_root).expanduser().resolve()
    recycle_parent = root / _RECYCLE_BIN
    batches_root = recycle_parent / _GC_BATCHES
    batch_root = batches_root / str(parsed_batch_id)
    if _has_link_component(batch_root, root):
        raise ValueError("recycle batch path contains a link")
    manifest_path = batch_root / "manifest.json"
    if _is_link(manifest_path) or not manifest_path.is_file():
        raise FileNotFoundError("recycle batch manifest was not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != 1
        or manifest.get("batch_id") != str(parsed_batch_id)
        or manifest.get("status") not in {"quarantining", "partial", "quarantined"}
        or not isinstance(manifest.get("items"), list)
    ):
        raise ValueError("recycle batch manifest is invalid")

    restore_items: list[tuple[dict[str, str], Path, Path]] = []
    for item in manifest["items"]:
        if not isinstance(item, dict) or item.get("status") not in {"moved", "moving"}:
            continue
        kind = item.get("kind")
        relative = _validated_gc_relative(item.get("source"))
        original = _workspace_gc_path(root, relative.as_posix(), kind)
        archive = batch_root / "payload" / Path(*relative.parts)
        if _has_link_component(archive, batch_root):
            raise ValueError("quarantined source cannot be a link")
        if not archive.exists():
            if item.get("status") == "moving" and original.exists():
                item["status"] = "pending"
                _write_gc_manifest(manifest_path, manifest)
                continue
            raise FileNotFoundError("quarantined source is missing")
        if _is_link(archive):
            raise ValueError("quarantined source cannot be a link")
        if original.exists() or original.is_symlink():
            raise FileExistsError(f"restore target already exists: {relative.as_posix()}")
        if _has_link_component(original.parent, root):
            raise ValueError("restore target path contains a link")
        restore_items.append((item, archive, original))

    restored_files = 0
    restored_dirs = 0
    for item, archive, original in restore_items:
        original.parent.mkdir(parents=True, exist_ok=True)
        if _has_link_component(original.parent, root):
            raise ValueError("restore target path contains a link")
        archive.replace(original)
        item["status"] = "restored"
        _write_gc_manifest(manifest_path, manifest)
        if item["kind"] == "file":
            restored_files += 1
        else:
            restored_dirs += 1

    manifest["status"] = "restored"
    _write_gc_manifest(manifest_path, manifest)
    return {
        "batch_id": str(parsed_batch_id),
        "restored_files": restored_files,
        "restored_dirs": restored_dirs,
    }


def _validate_gc_source(path: Path, namespace_root: Path, *, direct_child: bool) -> Path:
    lexical = Path(path)
    target = _validate_under(lexical, namespace_root, direct_child=direct_child)
    if _has_link_component(lexical, namespace_root):
        raise ValueError("cleanup source path contains a link")
    if target != lexical.resolve():
        raise ValueError("cleanup source path changed during audit")
    return lexical


def _validated_gc_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("workspace recycle path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("workspace recycle path is invalid")
    return relative


def _workspace_gc_path(root: Path, value: object, kind: object) -> Path:
    relative = _validated_gc_relative(value)
    parts = relative.parts
    if kind == "file":
        if len(parts) < 3 or parts[:2] != ("_docs", "attachments"):
            raise ValueError("only Docs attachment files can be restored")
        namespace_root = root / "_docs" / "attachments"
    elif kind == "directory":
        if len(parts) != 2 or parts[0] not in {"_projects", "_users"}:
            raise ValueError("only direct Project/User roots can be restored")
        expected_kind = "project" if parts[0] == "_projects" else "user"
        if not _NAMESPACE_ID.fullmatch(parts[1]) or not parts[1].startswith(expected_kind + "_"):
            raise ValueError("workspace owner namespace is invalid")
        namespace_root = root / parts[0]
    else:
        raise ValueError("workspace recycle item kind is invalid")
    target = root.joinpath(*parts)
    _validate_under(target, namespace_root, direct_child=kind == "directory")
    return target


def _has_link_component(path: Path, root: Path) -> bool:
    candidate = Path(path)
    boundary = Path(root)
    try:
        candidate.relative_to(boundary)
    except ValueError as exc:
        raise ValueError("workspace path escaped its boundary") from exc
    current = candidate
    while True:
        if _is_link(current):
            return True
        if current == boundary:
            return False
        current = current.parent


def _write_gc_manifest(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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
