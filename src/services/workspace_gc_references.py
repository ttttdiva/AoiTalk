"""Read-only reference inventory for workspace cleanup.

The cleanup command may quarantine only Docs attachments and unowned
Project/User roots. This inventory reads path-bearing columns and metadata
selectors from the other storage lifecycles so those references cannot be
mistaken for unowned workspace data. It never walks storage contents.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from uuid import UUID

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB

from ..memory.models import (
    AppActionRun,
    AppArtifact,
    AppJob,
    ConversationMessage,
    FileExplorerBookmark,
    FileExplorerLauncher,
    GeneratedMedia,
)
from .storage_roots import StorageRoot, read_registry

MAX_QA_MANIFEST_BYTES = 1_048_576
_QA_DATABASE = re.compile(r"aoitalk_test_employee_[0-9a-f]{32}\Z")
_PATH_KEYS = {
    "archive_dir",
    "artifact_dir",
    "artifact_path",
    "file_path",
    "log_path",
    "output_dir",
    "path",
    "relative_path",
    "staging_dir",
    "storage_path",
    "workspace_path",
}
_NAMESPACE_PREFIXES = {
    "_docs": "workspace_docs",
    "_projects": "workspace_project",
    "_users": "workspace_user",
    "_apps": "workspace_app",
    "_app_instances": "workspace_app_instance",
    "_app_artifacts": "workspace_app_artifact",
    "_app_action_files": "workspace_app_action_files",
}


@dataclass(frozen=True)
class QaManifest:
    path: Path
    category: str
    document: Mapping[str, Any]


@dataclass(frozen=True)
class WorkspaceReferenceInventory:
    """References relevant to GC plus a path-free classification summary."""

    workspace_references: tuple[str, ...]
    project_ids: tuple[str, ...]
    user_ids: tuple[str, ...]
    classification_counts: Mapping[str, int]


def chat_attachment_path_statement():
    """Select only the two persisted Chat path fields, never message text."""

    metadata = cast(ConversationMessage.message_metadata, JSONB)
    return select(
        func.jsonb_path_query_array(metadata, "$.attachments[*].path"),
        func.jsonb_path_query_array(
            metadata,
            "$.attachments[*].project_relative_path",
        ),
    )


def load_qa_manifest(path: str | os.PathLike[str], *, repository_root: Path) -> QaManifest:
    """Load one explicitly selected, bounded QA manifest from its owned location."""

    repo = Path(repository_root).expanduser().resolve()
    manifest_path = Path(os.path.abspath(Path(path).expanduser()))
    try:
        relative = manifest_path.relative_to(repo)
    except ValueError as exc:
        raise ValueError("QA manifest must be inside the current repository") from exc
    if (
        len(relative.parts) != 4
        or relative.name != "manifest.json"
        or not _QA_DATABASE.fullmatch(relative.parts[2])
    ):
        raise ValueError("QA manifest is outside the supported run layout")
    if relative.parts[:2] == (".local", "qa-runs"):
        category = "qa_staging"
    elif relative.parts[:2] == ("artifacts", "qa-runs"):
        category = "qa_archive"
    else:
        raise ValueError("QA manifest is outside the supported staging/archive roots")

    _reject_link_components(manifest_path)
    info = manifest_path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_QA_MANIFEST_BYTES:
        raise ValueError("QA manifest is not a bounded regular file")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("QA manifest is not valid UTF-8 JSON") from exc
    if not isinstance(document, Mapping):
        raise ValueError("QA manifest root must be an object")

    run_id = document.get("run_id")
    database = document.get("database")
    if (
        document.get("harness") != "ai_employee_qa"
        or document.get("schema") != "public"
        or database != relative.parts[2]
        or not isinstance(run_id, str)
        or database != "aoitalk_test_employee_" + _uuid_hex(run_id)
        or Path(os.path.abspath(Path(str(document.get("output_dir") or "")).expanduser()))
        != manifest_path.parent
        or Path(os.path.abspath(Path(str(document.get("repo") or "")).expanduser())) != repo
    ):
        raise ValueError("QA manifest identity/path mismatch")
    return QaManifest(path=manifest_path, category=category, document=document)


def build_workspace_reference_inventory(
    *,
    workspace_root: str | os.PathLike[str],
    repository_root: str | os.PathLike[str],
    chat_attachment_paths: Iterable[Any] = (),
    app_artifact_paths: Iterable[Any] = (),
    app_jobs: Iterable[tuple[Any, Any, Any]] = (),
    app_action_runs: Iterable[tuple[Any, Any, Any, Any]] = (),
    generated_media_paths: Iterable[Any] = (),
    file_explorer_paths: Iterable[Any] = (),
    external_roots: Iterable[StorageRoot] = (),
    qa_manifests: Iterable[QaManifest] = (),
    generated_media_root: str | os.PathLike[str] | None = None,
) -> WorkspaceReferenceInventory:
    """Classify narrow DB/config references without opening their target files."""

    root = Path(workspace_root).expanduser().absolute()
    repo = Path(repository_root).expanduser().absolute()
    roots = tuple(external_roots)
    external_paths = tuple(
        Path(item.root_path).expanduser().absolute()
        for item in roots
        if bool(item.external)
    )
    stage_root = repo / ".local" / "qa-runs"
    archive_root = repo / "artifacts" / "qa-runs"
    configured_media_root = (
        Path(generated_media_root).expanduser()
        if generated_media_root is not None
        else Path(os.environ.get("AOITALK_GENERATED_MEDIA_DIR", "data/generated_media"))
    )
    if not configured_media_root.is_absolute():
        configured_media_root = repo / configured_media_root

    project_ids: set[str] = set()
    user_ids: set[str] = set()
    workspace_paths: set[str] = set()
    classified_paths: dict[str, set[str]] = {}

    def add(raw: Any) -> None:
        if not isinstance(raw, (str, os.PathLike)):
            return
        value = os.fspath(raw).strip()
        if not value:
            return
        category, workspace_relative = _classify_path(
            value,
            workspace_root=root,
            external_roots=external_paths,
            generated_media_root=configured_media_root,
            qa_roots=(stage_root, archive_root),
        )
        normalized = value.replace("\\", "/")
        classification_key = workspace_relative or (
            os.path.normcase(os.path.abspath(value))
            if Path(value).expanduser().is_absolute()
            else normalized
        )
        classified_paths.setdefault(category, set()).add(classification_key.casefold())
        if workspace_relative is not None:
            workspace_paths.add(workspace_relative)
        owner_relative = workspace_relative or _raw_workspace_relative(value, root)
        if owner_relative is not None:
            _collect_namespace_owner_ids(owner_relative, project_ids=project_ids, user_ids=user_ids)

    for path in chat_attachment_paths:
        add(path)
    for path in app_artifact_paths:
        add(path)
    for app_id, project_id, log_path in app_jobs:
        _add_app_identity_paths(add, app_id=app_id, project_id=project_id)
        add(log_path)
    for run_id, app_id, project_id, log_path in app_action_runs:
        _add_app_identity_paths(add, app_id=app_id, project_id=project_id)
        add(log_path)
        try:
            run_uuid = UUID(str(run_id))
        except (TypeError, ValueError, AttributeError):
            continue
        add(f"_app_action_files/runs/run_{run_uuid}")
    for path in generated_media_paths:
        add(path)
    for path in file_explorer_paths:
        add(path)

    for storage_root in roots:
        if not bool(storage_root.external):
            continue
        add(storage_root.root_path)
        project_ids.update(_valid_ids(storage_root.project_ids))
        user_ids.update(_valid_ids(storage_root.user_ids))

    for manifest in qa_manifests:
        add(manifest.path)
        for path in _manifest_path_values(manifest.document):
            add(path)
        fixtures = manifest.document.get("fixtures")
        if isinstance(fixtures, Mapping):
            project_ids.update(_valid_ids((fixtures.get("project_id"),)))
            user_ids.update(_valid_ids((fixtures.get("user_id"),)))

    return WorkspaceReferenceInventory(
        workspace_references=tuple(sorted(workspace_paths, key=str.casefold)),
        project_ids=tuple(sorted(project_ids)),
        user_ids=tuple(sorted(user_ids)),
        classification_counts={
            key: len(values)
            for key, values in sorted(classified_paths.items())
        },
    )


async def load_workspace_reference_inventory(
    session,
    *,
    workspace_root: str | os.PathLike[str],
    repository_root: str | os.PathLike[str],
    qa_manifest_path: str | os.PathLike[str] | None = None,
) -> WorkspaceReferenceInventory:
    """Read the existing path columns and configuration in one DB snapshot.

    Chat SQL extracts only the ``path`` member from attachment entries. App
    jobs/runs contribute their explicit log columns and run identity; their
    input/result payloads are deliberately not read.
    """

    chat_result = await session.execute(chat_attachment_path_statement())
    chat_batches = chat_result.all()
    chat_paths = tuple(
        path
        for row in chat_batches
        for batch in row
        for path in (batch if isinstance(batch, list) else ())
        if isinstance(path, str)
    )
    artifact_paths = (
        await session.execute(select(AppArtifact.file_path))
    ).scalars().all()
    app_jobs = (await session.execute(
        select(AppJob.app_id, AppJob.project_id, AppJob.log_path)
    )).all()
    action_runs = (
        await session.execute(
            select(AppActionRun.id, AppActionRun.app_id, AppActionRun.project_id, AppActionRun.log_path)
        )
    ).all()
    media_paths = (
        await session.execute(select(GeneratedMedia.relative_path))
    ).scalars().all()
    bookmark_paths = (
        await session.execute(select(FileExplorerBookmark.path))
    ).scalars().all()
    launcher_paths = (
        await session.execute(select(FileExplorerLauncher.path))
    ).scalars().all()

    manifests: tuple[QaManifest, ...] = ()
    if qa_manifest_path:
        manifests = (
            load_qa_manifest(qa_manifest_path, repository_root=Path(repository_root)),
        )

    return build_workspace_reference_inventory(
        workspace_root=workspace_root,
        repository_root=repository_root,
        chat_attachment_paths=chat_paths,
        app_artifact_paths=artifact_paths,
        app_jobs=app_jobs,
        app_action_runs=action_runs,
        generated_media_paths=media_paths,
        file_explorer_paths=(*bookmark_paths, *launcher_paths),
        external_roots=read_registry().roots,
        qa_manifests=manifests,
    )


def _manifest_path_values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            normalized_key = str(child_key).casefold()
            if normalized_key in _PATH_KEYS and isinstance(child, str):
                yield child
            elif isinstance(child, (Mapping, list, tuple)):
                yield from _manifest_path_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            if isinstance(child, (Mapping, list, tuple)):
                yield from _manifest_path_values(child)


def _classify_path(
    value: str,
    *,
    workspace_root: Path,
    external_roots: tuple[Path, ...],
    generated_media_root: Path,
    qa_roots: tuple[Path, Path],
) -> tuple[str, str | None]:
    if value.startswith("remote://"):
        return "remote_reference", None
    if value.startswith("aoitalk-record-table:"):
        return "virtual_reference", None

    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        for index, qa_root in enumerate(qa_roots):
            if _lexical_relative(raw_path, qa_root) is not None:
                return ("qa_staging" if index == 0 else "qa_archive"), None
        relative = _lexical_relative(raw_path, workspace_root)
        if relative is not None:
            return _classify_workspace_relative(relative)
        if _lexical_relative(raw_path, generated_media_root) is not None:
            return "generated_media", None
        if any(_lexical_relative(raw_path, external_root) is not None for external_root in external_roots):
            return "external_files", None
        return "absolute_unclassified", None

    normalized = value.replace("\\", "/").strip("/")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if any(part == ".." for part in parts):
        # Still preserve an unambiguous owner root before the traversal marker.
        relative = "/".join(parts)
        category, _ = _classify_workspace_relative(relative)
        return category, None
    if parts and (parts[0] == workspace_root.name or parts[0] == "workspaces"):
        parts = parts[1:]
    if parts and parts[0] == "generated_media":
        return "generated_media", None
    if parts and parts[0] in _NAMESPACE_PREFIXES:
        return _classify_workspace_relative("/".join(parts))
    return "relative_unclassified", None


def _classify_workspace_relative(relative: str) -> tuple[str, str]:
    parts = tuple(part for part in relative.replace("\\", "/").split("/") if part)
    category = _NAMESPACE_PREFIXES.get(parts[0], "workspace_other") if parts else "workspace_other"
    if len(parts) >= 2 and parts[0] == "_app_action_files" and parts[1] == "runs":
        category = "workspace_app_run"
    elif len(parts) >= 2 and parts[0] == "_app_action_files":
        category = "workspace_app_action_files"
    return category, "/".join(parts)


def _collect_namespace_owner_ids(
    relative: str,
    *,
    project_ids: set[str],
    user_ids: set[str],
) -> None:
    parts = tuple(part for part in relative.split("/") if part)
    candidates: list[tuple[str, str]] = []
    if len(parts) >= 2 and parts[0] in {"_projects", "_app_instances"}:
        candidates.append(("project", parts[1]))
    if len(parts) >= 2 and parts[0] == "_users":
        candidates.append(("user", parts[1]))
    for kind, segment in candidates:
        expected_prefix = kind + "_"
        if not segment.startswith(expected_prefix):
            continue
        parsed = _valid_ids((segment[len(expected_prefix):],))
        if parsed:
            (project_ids if kind == "project" else user_ids).update(parsed)


def _add_app_identity_paths(add, *, app_id: Any, project_id: Any) -> None:
    try:
        app_uuid = UUID(str(app_id))
    except (TypeError, ValueError, AttributeError):
        return
    add(f"_apps/app_{app_uuid}")
    try:
        project_uuid = UUID(str(project_id))
    except (TypeError, ValueError, AttributeError):
        return
    add(f"_app_instances/project_{project_uuid}/app_{app_uuid}")


def _raw_workspace_relative(value: str, workspace_root: Path) -> str | None:
    path = Path(value).expanduser()
    if path.is_absolute():
        return _lexical_relative(path, workspace_root)
    parts = tuple(part for part in value.replace("\\", "/").strip("/").split("/") if part)
    if parts and parts[0] in {workspace_root.name, "workspaces"}:
        parts = parts[1:]
    if parts and parts[0] in _NAMESPACE_PREFIXES:
        return "/".join(parts)
    return None


def _valid_ids(values: Iterable[Any]) -> set[str]:
    parsed: set[str] = set()
    for value in values:
        try:
            parsed.add(str(UUID(str(value))))
        except (TypeError, ValueError, AttributeError):
            continue
    return parsed


def _uuid_hex(value: str) -> str:
    try:
        return UUID(value).hex
    except (TypeError, ValueError, AttributeError):
        return ""


def _lexical_relative(path: Path, root: Path) -> str | None:
    """Return a lexical relative path without resolving or opening either path."""

    try:
        target = os.path.normcase(os.path.abspath(os.fspath(path)))
        boundary = os.path.normcase(os.path.abspath(os.fspath(root)))
        if os.path.commonpath((target, boundary)) != boundary:
            return None
        relative = os.path.relpath(target, boundary)
    except (OSError, ValueError):
        return None
    return "" if relative == os.curdir else PurePosixPath(*Path(relative).parts).as_posix()


def _reject_link_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            raise ValueError("QA manifest path is missing") from None
        except OSError as exc:
            raise ValueError("QA manifest path cannot be checked") from exc
        is_junction = getattr(current, "is_junction", None)
        if (
            stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            or (callable(is_junction) and is_junction())
        ):
            raise ValueError("QA manifest path cannot contain links")
