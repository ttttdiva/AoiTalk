"""Mediated publication of sandboxed Project storage diffs.

The command/sandbox side of an agent must never write directly to the
``_projects`` namespace.  It hands this module a *structured* diff and an
opaque, server-created staging capability instead.  The publisher is the
single mutation boundary for Project files and therefore owns the complete
transaction:

* acquire the shared Project operation lock;
* lock and re-check the Project row and current ACLs;
* canonicalise every project-relative path (including link/reparse checks);
* calculate and enforce the strict quota projection;
* journal filesystem changes before applying them; and
* commit the DB counter only after the filesystem is durable, restoring the
  journal when either side fails.

No path in a model/tool payload is used as an authority.  The only path the
diff can name is a relative destination under the locked Project root; staged
content is resolved from a trusted ``ProjectStorageCapability`` (or supplied
as immutable bytes by a trusted caller).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from ..memory.models import ProjectStorageOperation
from ..memory.project_repository import ProjectRepository
from .app_operation_lock import project_operation_lock
from .app_storage import get_workspaces_root

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProjectStoragePublishError(RuntimeError):
    """Base class for mediated Project storage publication failures."""


class ProjectStoragePathError(ProjectStoragePublishError, ValueError):
    """A destination/source path is malformed or outside its namespace."""


class ProjectStoragePermissionError(ProjectStoragePublishError, PermissionError):
    """The authenticated principal lacks the required Project capability."""


class ProjectStorageQuotaError(ProjectStoragePublishError, ValueError):
    """A diff would exceed the current Project storage quota."""


class ProjectStorageManagedPathError(ProjectStoragePublishError, ValueError):
    """A path is a known managed/reference file and cannot be overwritten."""


class ProjectStorageIdempotencyError(ProjectStoragePublishError, ValueError):
    """An idempotency key was reused for a different diff."""


class ProjectStorageRollbackError(ProjectStoragePublishError):
    """Publication failed and restoring the filesystem journal also failed."""


# ---------------------------------------------------------------------------
# Trusted inputs and structured diff
# ---------------------------------------------------------------------------


_CAPABILITY_TOKEN = object()


def _coerce_uuid(value: Any, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProjectStoragePermissionError(f"{label} must be a UUID") from exc


def _normalise_principal_id(principal: Any) -> UUID:
    """Resolve only an identity from a trusted principal object.

    ``dict`` values are accepted for compatibility with request principal
    adapters, but no path/capability field from a mapping is ever consumed.
    The caller's DB ACL remains authoritative.
    """

    if isinstance(principal, UUID):
        return principal
    if isinstance(principal, (str, bytes)):
        return _coerce_uuid(principal, "principal")
    if isinstance(principal, Mapping):
        value = principal.get("user_id", principal.get("id"))
        # A mapping that attempts to smuggle a root is not a trusted
        # capability object.  Reject it rather than silently ignoring the
        # suspicious authority-looking fields.
        if any(key in principal for key in ("staged_root", "workspace_root", "root", "write_roots", "delete_roots")):
            raise ProjectStoragePermissionError("principal mappings cannot carry filesystem authority")
        return _coerce_uuid(value, "principal.user_id")
    for attr in ("user_id", "principal_id", "id"):
        value = getattr(principal, attr, None)
        if value is not None:
            return _coerce_uuid(value, f"principal.{attr}")
    raise ProjectStoragePermissionError("a trusted principal with user_id is required")


@dataclass(frozen=True, slots=True)
class ProjectStorageCapability:
    """Opaque server-issued authority for one staged Project diff.

    ``issue`` is the only public constructor that can create a valid
    capability.  A model may reproduce the visible fields but cannot provide
    the private token, and callers cannot change the immutable staged root or
    Project/principal binding after issuance.
    """

    principal_id: UUID
    project_id: UUID
    staged_root: Path
    allow_write: bool = True
    allow_delete: bool = False
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        principal_id = _coerce_uuid(self.principal_id, "principal_id")
        project_id = _coerce_uuid(self.project_id, "project_id")
        lexical = Path(os.path.abspath(os.fspath(Path(self.staged_root).expanduser())))
        # Keep the lexical check before resolving so a staged-root symlink
        # cannot be hidden by the canonical path stored in the capability.
        _assert_no_reparse_components(lexical)
        root = Path(os.path.realpath(os.fspath(lexical)))
        if self._token is not _CAPABILITY_TOKEN:
            raise ProjectStoragePermissionError("invalid Project storage capability")
        if not root.is_absolute():
            raise ProjectStoragePermissionError("staged root must be canonical")
        if not self.allow_write and self.allow_delete:
            # A delete-only sandbox is valid.  This branch documents that
            # write/delete are independent rather than tying delete to write.
            pass
        object.__setattr__(self, "principal_id", principal_id)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "staged_root", root)

    @classmethod
    def issue(
        cls,
        *,
        principal_id: UUID | str,
        project_id: UUID | str,
        staged_root: str | os.PathLike[str],
        allow_write: bool = True,
        allow_delete: bool = False,
    ) -> "ProjectStorageCapability":
        """Issue a capability from trusted server-side context."""

        return cls(
            principal_id=_coerce_uuid(principal_id, "principal_id"),
            project_id=_coerce_uuid(project_id, "project_id"),
            staged_root=Path(staged_root),
            allow_write=bool(allow_write),
            allow_delete=bool(allow_delete),
            _token=_CAPABILITY_TOKEN,
        )


# Names used by future harness integrations.  Keep one implementation while
# allowing callers to use whichever domain term they already use.
TrustedProjectStorageCapability = ProjectStorageCapability
ProjectPublishCapability = ProjectStorageCapability


def issue_project_storage_capability(**kwargs: Any) -> ProjectStorageCapability:
    """Server-side factory for :class:`ProjectStorageCapability`."""

    return ProjectStorageCapability.issue(**kwargs)


@dataclass(frozen=True, slots=True)
class StagedFile:
    """One trusted staged source or immutable content payload."""

    source: str | os.PathLike[str] | None = None
    content: bytes | bytearray | memoryview | str | None = None
    encoding: str = "utf-8"

    def __post_init__(self) -> None:
        if self.source is None and self.content is None:
            raise ValueError("StagedFile requires source or content")
        if self.source is not None and self.content is not None:
            raise ValueError("StagedFile source and content are mutually exclusive")


StagedContent = StagedFile


@dataclass(frozen=True, slots=True)
class ProjectStorageDiff:
    """Structured file diff produced by a trusted harness publisher.

    ``writes`` maps project-relative destination names to ``StagedFile``, a
    staged-root-relative path, a ``Path``, or immutable bytes/text.  Mapping
    payloads from a model are normalised but never allowed to carry a root;
    the root comes solely from ``ProjectStorageCapability``/publisher setup.
    """

    writes: Mapping[str, Any] = field(default_factory=dict)
    deletes: Sequence[str] = field(default_factory=tuple)
    operation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.writes, Mapping):
            raise TypeError("writes must be a mapping of relative paths")
        if isinstance(self.deletes, (str, bytes)):
            raise TypeError("deletes must be a sequence of relative paths")


StagedProjectDiff = ProjectStorageDiff
ProjectPublishDiff = ProjectStorageDiff


@dataclass(frozen=True, slots=True)
class ProjectStoragePublishResult(Mapping[str, Any]):
    """Immutable publication result with mapping compatibility."""

    project_id: UUID
    principal_id: UUID
    operation_id: str | None
    writes: tuple[str, ...]
    deletes: tuple[str, ...]
    changed: bool
    idempotent: bool
    storage_used_mb: float
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": str(self.project_id),
            "principal_id": str(self.principal_id),
            "operation_id": self.operation_id,
            "writes": list(self.writes),
            "deletes": list(self.deletes),
            "changed": self.changed,
            "idempotent": self.idempotent,
            "storage_used_mb": self.storage_used_mb,
            "total_bytes": self.total_bytes,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


# Completed operation records intentionally remain process-local.  Durable
# AgentRun evidence can persist the operation key/hash; this cache only makes
# retries in one worker inexpensive and prevents duplicate filesystem work.
_IDEMPOTENCY_LOCK = threading.Lock()
_COMPLETED_OPERATIONS: dict[tuple[UUID, UUID, str], tuple[str, ProjectStoragePublishResult]] = {}
_MAX_COMPLETED_OPERATIONS = 4096
_MAX_DIFF_ENTRIES = 1000
_MAX_DIFF_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Path/metadata helpers
# ---------------------------------------------------------------------------


_RESERVED_COMPONENTS = {".git", ".trash"}
_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_PATH_HINTS = {
    "path",
    "paths",
    "file",
    "files",
    "file_path",
    "target_path",
    "source_path",
    "managed",
    "managed_path",
    "managed_paths",
    "reference",
    "references",
    "reference_path",
    "reference_paths",
    "canonical",
    "canonical_path",
    "canonical_paths",
    "wbs_file",
    "status_file",
    "issue_file",
    "risk_file",
    "request_files",
}


def _path_key(value: str) -> str:
    return os.path.normcase(value.replace("\\", "/").strip("/")).replace(
        "\\",
        "/",
    )


def _conflicts_with_managed_path(candidate: str, managed_paths: set[str]) -> bool:
    key = _path_key(candidate)
    prefix = key + "/"
    return key in managed_paths or any(
        managed.startswith(prefix) for managed in managed_paths
    )


def _normalise_relative_path(value: Any, *, allow_root: bool = False) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw:
        if allow_root:
            return ""
        raise ProjectStoragePathError("project-relative path is required")
    if "\x00" in raw or raw.startswith(("/", "//")):
        raise ProjectStoragePathError("absolute/NUL project paths are not allowed")
    # ``PurePosixPath`` preserves a drive-like first component, so check it
    # explicitly before normalising.
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise ProjectStoragePathError("drive-qualified paths are not allowed")
    parts = tuple(part for part in raw.split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise ProjectStoragePathError("path traversal is not allowed")
    if any(part.casefold() in _RESERVED_COMPONENTS for part in parts):
        raise ProjectStoragePathError(".git/.trash paths are not publishable")
    if os.name == "nt":
        for part in parts:
            if ":" in part or part.rstrip(" .") != part:
                raise ProjectStoragePathError(
                    "Windows alias/alternate-stream paths are not publishable"
                )
            stem = part.split(".", 1)[0].casefold()
            if stem in _WINDOWS_RESERVED_NAMES:
                raise ProjectStoragePathError(
                    "Windows reserved device paths are not publishable"
                )
    return "/".join(parts)


def _canonical_relative_key(project_root: Path, destination: Path) -> str:
    try:
        relative = destination.relative_to(project_root).as_posix()
    except ValueError as exc:
        raise ProjectStoragePathError(
            "canonical destination escaped Project storage root"
        ) from exc
    parts = tuple(part.casefold() for part in Path(relative).parts)
    if any(part in _RESERVED_COMPONENTS for part in parts):
        raise ProjectStoragePathError(".git/.trash paths are not publishable")
    return _path_key(relative)


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        # Uninspectable is fail closed: a hidden reparse point must not be
        # treated as an ordinary directory.
        return True
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _existing_components(path: Path) -> Iterable[Path]:
    anchor = Path(path.anchor) if path.anchor else Path.cwd()
    current = anchor
    try:
        parts = path.relative_to(anchor).parts
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
            yield current
            break
        yield current


def _assert_no_reparse_components(path: Path, *, root: Path | None = None) -> None:
    for component in _existing_components(path):
        if _is_reparse_or_symlink(component):
            raise ProjectStoragePathError(f"symlink/junction/reparse path is not allowed: {component}")
    if root is not None:
        candidate = Path(os.path.realpath(os.path.abspath(path)))
        canonical_root = Path(os.path.realpath(os.path.abspath(root)))
        try:
            candidate.relative_to(canonical_root)
        except ValueError as exc:
            raise ProjectStoragePathError("path escaped Project storage root") from exc


def _assert_safe_root(root: Path) -> Path:
    # Inspect the lexical path *before* ``realpath``.  Resolving first would
    # hide that the configured root itself (or one of its ancestors) is a
    # symlink/junction and would turn an explicit link rejection into an
    # accidental trust decision.
    lexical = Path(os.path.abspath(os.fspath(Path(root).expanduser())))
    if not lexical.is_absolute():
        raise ProjectStoragePathError("workspace root must be absolute")
    _assert_no_reparse_components(lexical)
    canonical = Path(os.path.realpath(os.fspath(lexical)))
    _assert_no_reparse_components(canonical)
    canonical.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(canonical)
    return canonical


def _strict_storage_usage(root: Path) -> int:
    """Count regular bytes while rejecting every link/reparse entry."""

    _assert_no_reparse_components(root)
    if not root.exists():
        return 0
    if not root.is_dir():
        raise ProjectStoragePathError("Project storage root is not a directory")
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        _assert_no_reparse_components(current)
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise ProjectStoragePublishError(f"cannot scan Project storage: {current}") from exc
        for entry in entries:
            child = Path(entry.path)
            if _is_reparse_or_symlink(child):
                raise ProjectStoragePathError(f"Project storage contains a link/reparse entry: {child}")
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(child)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                else:
                    raise ProjectStoragePathError(f"unsupported Project storage entry: {child}")
            except OSError as exc:
                raise ProjectStoragePublishError(f"cannot inspect Project storage entry: {child}") from exc
    return total


def _canonical_project_root(
    workspace_root: Path,
    storage_relative: str,
    *,
    project_id: UUID | None = None,
) -> Path:
    relative = _normalise_workspace_storage_relative(storage_relative)
    if project_id is not None:
        expected_name = f"project_{project_id}"
        actual_name = relative.split("/", 1)[1]
        if actual_name.casefold() != expected_name.casefold():
            raise ProjectStoragePathError("Project storage namespace does not match project_id")
    lexical_root = workspace_root / Path(*relative.split("/"))
    # Check lexical components first; otherwise resolving a malicious
    # ``_projects`` symlink could make an outside directory appear canonical.
    _assert_no_reparse_components(lexical_root, root=workspace_root)
    root = Path(os.path.realpath(os.fspath(lexical_root)))
    try:
        root.relative_to(workspace_root)
    except ValueError as exc:
        raise ProjectStoragePathError("Project storage root escaped workspace root") from exc
    if root.name.casefold() == ".git" or root.name.casefold() == ".trash":
        raise ProjectStoragePathError("invalid Project storage root")
    _assert_no_reparse_components(root, root=workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(root, root=workspace_root)
    return root


def _normalise_workspace_storage_relative(value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip("/")
    parts = tuple(part for part in raw.split("/") if part)
    if len(parts) != 2 or parts[0].casefold() != "_projects" or not parts[1].casefold().startswith("project_"):
        raise ProjectStoragePathError("unexpected Project storage namespace")
    if any(part in {".", ".."} for part in parts):
        raise ProjectStoragePathError("invalid Project storage namespace")
    return "/".join(parts)


def _canonical_destination(root: Path, relative: str) -> Path:
    path = root / Path(*relative.split("/"))
    # Validate the lexical root before resolving.  ``resolve`` is still used
    # for containment, but symlink components are rejected rather than
    # allowing an in-root alias.
    _assert_no_reparse_components(path, root=root)
    canonical = Path(os.path.realpath(os.path.abspath(path)))
    try:
        canonical.relative_to(root)
    except ValueError as exc:
        raise ProjectStoragePathError("destination escaped Project storage root") from exc
    if canonical == root:
        raise ProjectStoragePathError("Project storage root cannot be mutated")
    return canonical


def _metadata_paths(value: Any, *, key: str = "") -> Iterable[str]:
    """Yield path-looking metadata values without trusting their authority."""

    key_folded = key.casefold().replace("-", "_")
    hinted = key_folded in _PATH_HINTS or any(token in key_folded for token in ("path", "file", "reference", "managed", "canonical"))
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            yield from _metadata_paths(child, key=str(child_key))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            # List values under a path hint are path candidates; nested
            # mappings are recursively examined for their own hints.
            if hinted and isinstance(child, (str, os.PathLike)):
                yield str(child)
            else:
                yield from _metadata_paths(child, key=key)
    elif hinted and isinstance(value, (str, os.PathLike)):
        yield str(value)


def _normalise_reference_path(value: Any, project_id: UUID) -> str | None:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw:
        return None
    prefix = f"_projects/project_{project_id}/"
    absolute_marker = "/" + prefix
    marker_index = raw.casefold().find(absolute_marker.casefold())
    if marker_index >= 0:
        raw = raw[marker_index + len(absolute_marker) :]
    elif raw.startswith(("/", "//")) or (
        len(raw) >= 2 and raw[1] == ":"
    ):
        return None
    if raw.casefold().startswith(prefix.casefold()):
        raw = raw[len(prefix) :]
    try:
        return _normalise_relative_path(raw)
    except ProjectStoragePathError:
        return None


async def _load_known_reference_paths(session: Any, project_id: UUID) -> set[str]:
    """Collect every durable managed attachment/reference path fail-closed."""

    try:
        from ..memory.models import (
            RecordAttachment,
            RecordRow,
            TaskAttachment,
            TaskReference,
        )

        statements = (
            select(TaskReference.target_path).where(
                TaskReference.project_id == project_id,
                TaskReference.target_path.is_not(None),
            ),
            select(TaskAttachment.file_path).where(
                TaskAttachment.project_id == project_id,
                TaskAttachment.file_path.is_not(None),
            ),
            select(RecordAttachment.file_path)
            .join(RecordRow, RecordAttachment.row_id == RecordRow.id)
            .where(
                RecordRow.project_id == project_id,
                RecordAttachment.file_path.is_not(None),
            ),
        )
        values: list[Any] = []
        for statement in statements:
            result = await session.execute(statement)
            values.extend(result.scalars().all())
    except Exception as exc:
        raise ProjectStorageManagedPathError(
            "durable Project managed-path lookup failed"
        ) from exc
    return {
        key
        for value in values
        if (key := _normalise_reference_path(value, project_id)) is not None
    }


# ---------------------------------------------------------------------------
# Diff normalisation and journal
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PreparedWrite:
    relative: str
    content: bytes


@dataclass(slots=True)
class _JournalEntry:
    target: Path
    backup: Path | None
    existed: bool
    is_directory: bool = False


@dataclass(slots=True)
class _FilesystemJournal:
    root: Path
    project_id: str = ""
    principal_id: str = ""
    operation_id: str = ""
    diff_sha256: str = ""
    entries: list[_JournalEntry] = field(default_factory=list)
    created_dirs: list[Path] = field(default_factory=list)
    committed: bool = False

    @classmethod
    def create(
        cls,
        workspace_root: Path,
        operation_id: str,
        *,
        project_id: UUID,
        principal_id: UUID,
        diff_sha256: str,
    ) -> "_FilesystemJournal":
        journal_base = workspace_root / ".project-publish-journal"
        _assert_no_reparse_components(journal_base, root=workspace_root)
        journal_base.mkdir(parents=True, exist_ok=True)
        _assert_no_reparse_components(journal_base, root=workspace_root)
        root = Path(tempfile.mkdtemp(prefix=f"{operation_id}-", dir=journal_base))
        _assert_no_reparse_components(root, root=workspace_root)
        (root / "backups").mkdir()
        journal = cls(
            root=root,
            project_id=str(project_id),
            principal_id=str(principal_id),
            operation_id=str(operation_id),
            diff_sha256=str(diff_sha256),
        )
        journal._write_manifest()
        return journal

    @classmethod
    def load(cls, root: Path, *, workspace_root: Path) -> "_FilesystemJournal":
        _assert_no_reparse_components(root, root=workspace_root)
        manifest_path = root / "manifest.json"
        _assert_no_reparse_components(manifest_path, root=workspace_root)
        if not manifest_path.is_file() or manifest_path.stat().st_size > 1_048_576:
            raise ProjectStorageRollbackError(
                f"Project publication journal manifest is unavailable: {root}"
            )
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectStorageRollbackError(
                f"Project publication journal manifest is invalid: {root}"
            ) from exc
        if not isinstance(payload, Mapping) or payload.get("version") != 1:
            raise ProjectStorageRollbackError(
                f"Project publication journal manifest is invalid: {root}"
            )
        journal = cls(
            root=root,
            project_id=str(payload.get("project_id") or ""),
            principal_id=str(payload.get("principal_id") or ""),
            operation_id=str(payload.get("operation_id") or ""),
            diff_sha256=str(payload.get("diff_sha256") or ""),
        )
        try:
            UUID(journal.project_id)
            UUID(journal.principal_id)
        except (TypeError, ValueError) as exc:
            raise ProjectStorageRollbackError(
                f"Project publication journal identity is invalid: {root}"
            ) from exc
        if not re.fullmatch(r"[0-9a-f]{64}", journal.diff_sha256):
            raise ProjectStorageRollbackError(
                f"Project publication journal digest is invalid: {root}"
            )
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or len(raw_entries) > _MAX_DIFF_ENTRIES:
            raise ProjectStorageRollbackError(
                f"Project publication journal entries are invalid: {root}"
            )
        for raw in raw_entries:
            if not isinstance(raw, Mapping):
                raise ProjectStorageRollbackError("Project publication journal entry is invalid")
            target = Path(str(raw.get("target") or ""))
            try:
                target.relative_to(workspace_root)
            except ValueError as exc:
                raise ProjectStorageRollbackError(
                    "Project publication journal target escaped workspace"
                ) from exc
            backup_value = raw.get("backup")
            backup = None
            if backup_value:
                backup = root / Path(str(backup_value))
                try:
                    backup.relative_to(root)
                except ValueError as exc:
                    raise ProjectStorageRollbackError(
                        "Project publication journal backup escaped journal"
                    ) from exc
                _assert_no_reparse_components(backup, root=root)
            journal.entries.append(
                _JournalEntry(
                    target=target,
                    backup=backup,
                    existed=raw.get("existed") is True,
                    is_directory=raw.get("is_directory") is True,
                )
            )
        raw_dirs = payload.get("created_dirs") or []
        if not isinstance(raw_dirs, list) or len(raw_dirs) > _MAX_DIFF_ENTRIES * 8:
            raise ProjectStorageRollbackError("Project publication journal directories are invalid")
        for value in raw_dirs:
            directory = Path(str(value or ""))
            try:
                directory.relative_to(workspace_root)
            except ValueError as exc:
                raise ProjectStorageRollbackError(
                    "Project publication journal directory escaped workspace"
                ) from exc
            journal.created_dirs.append(directory)
        return journal

    def _write_manifest(self) -> None:
        payload = {
            "version": 1,
            "project_id": self.project_id,
            "principal_id": self.principal_id,
            "operation_id": self.operation_id,
            "diff_sha256": self.diff_sha256,
            "entries": [
                {
                    "target": str(entry.target),
                    "backup": (
                        entry.backup.relative_to(self.root).as_posix()
                        if entry.backup is not None
                        else None
                    ),
                    "existed": entry.existed,
                    "is_directory": entry.is_directory,
                }
                for entry in self.entries
            ],
            "created_dirs": [str(item) for item in self.created_dirs],
        }
        temporary = self.root / ".manifest.tmp"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.root / "manifest.json")

    def backup_target(self, target: Path, *, workspace_root: Path) -> None:
        if any(entry.target == target for entry in self.entries):
            return
        _assert_no_reparse_components(target, root=workspace_root)
        existed = target.exists() or target.is_symlink()
        backup: Path | None = None
        is_directory = False
        if existed:
            if _is_reparse_or_symlink(target):
                raise ProjectStoragePathError(f"cannot journal link/reparse target: {target}")
            is_directory = target.is_dir()
            backup = self.root / "backups" / f"{len(self.entries):08d}"
            if is_directory:
                _copy_tree_no_links(target, backup)
            else:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
        self.entries.append(_JournalEntry(target, backup, existed, is_directory))
        # Persist recovery metadata before the caller mutates the target.
        self._write_manifest()

    def record_created_dir(self, directory: Path) -> None:
        if directory not in self.created_dirs:
            self.created_dirs.append(directory)
            self._write_manifest()

    def restore(self, *, workspace_root: Path) -> None:
        failures: list[BaseException] = []
        for entry in reversed(self.entries):
            try:
                _assert_no_reparse_components(entry.target, root=workspace_root)
                if entry.target.exists() or entry.target.is_symlink():
                    if _is_reparse_or_symlink(entry.target):
                        raise ProjectStoragePathError(f"cannot restore through link/reparse target: {entry.target}")
                    if entry.target.is_dir():
                        shutil.rmtree(entry.target)
                    else:
                        entry.target.unlink()
                if entry.existed and entry.backup is not None:
                    entry.target.parent.mkdir(parents=True, exist_ok=True)
                    if entry.is_directory:
                        _copy_tree_no_links(entry.backup, entry.target)
                    else:
                        os.replace(entry.backup, entry.target)
            except BaseException as exc:  # pragma: no cover - exercised by fault injection
                failures.append(exc)
        for directory in sorted(self.created_dirs, key=lambda item: len(item.parts), reverse=True):
            try:
                if directory.exists() and not any(directory.iterdir()):
                    directory.rmdir()
            except OSError as exc:
                failures.append(exc)
        if failures:
            raise ProjectStorageRollbackError(
                f"failed to restore {len(failures)} Project filesystem journal entries"
            ) from failures[0]

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        parent = self.root.parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            logger.warning("failed to remove Project publication journal root: %s", parent)


def _copy_tree_no_links(source: Path, destination: Path) -> None:
    _assert_no_reparse_components(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=False)
    for item in source.iterdir():
        if _is_reparse_or_symlink(item):
            raise ProjectStoragePathError(f"journal encountered link/reparse entry: {item}")
        target = destination / item.name
        if item.is_dir():
            _copy_tree_no_links(item, target)
        elif item.is_file():
            shutil.copy2(item, target)
        else:
            raise ProjectStoragePathError(f"unsupported journal entry: {item}")


def _ensure_parent_directory(parent: Path, *, root: Path, journal: _FilesystemJournal) -> None:
    _assert_no_reparse_components(parent, root=root)
    if parent.exists():
        if not parent.is_dir():
            raise ProjectStoragePathError(f"destination parent is not a directory: {parent}")
        return
    missing: list[Path] = []
    current = parent
    while not current.exists():
        missing.append(current)
        if current == root:
            break
        current = current.parent
    if root not in [root_candidate for root_candidate in (current, *current.parents)]:
        raise ProjectStoragePathError("destination parent escaped Project root")
    for directory in reversed(missing):
        directory.mkdir()
        _assert_no_reparse_components(directory, root=root)
        journal.record_created_dir(directory)


def _coerce_staged_bytes(value: Any, *, staged_root: Path | None) -> bytes:
    if isinstance(value, StagedFile):
        if value.content is not None:
            content = value.content
            if isinstance(content, str):
                return content.encode(value.encoding)
            return bytes(content)
        value = value.source
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, Path):
        source = value
    elif isinstance(value, Mapping):
        # Explicit ``content`` is immutable payload; explicit source names a
        # path relative to the trusted staging root.  A root-looking field is
        # rejected instead of being treated as authority.
        if any(key in value for key in ("root", "staged_root", "workspace_root")):
            raise ProjectStoragePathError("staged write cannot supply a root")
        if "content" in value or "bytes" in value:
            content = value.get("content", value.get("bytes"))
            if isinstance(content, str):
                return content.encode(str(value.get("encoding") or "utf-8"))
            if isinstance(content, (bytes, bytearray, memoryview)):
                return bytes(content)
            raise ProjectStoragePathError("staged content must be bytes or text")
        source_value = value.get("source", value.get("staged_path", value.get("path")))
        if source_value is None:
            raise ProjectStoragePathError("staged write requires source or content")
        value = source_value
        source = Path(str(value))
    elif isinstance(value, str):
        if staged_root is None:
            # A plain string is data only when wrapped as StagedFile/content;
            # treating it as a path without a trusted root would create a
            # filesystem authority side channel.
            raise ProjectStoragePathError("staged source root is required for string write values")
        source = Path(value)
    else:
        raise ProjectStoragePathError("unsupported staged write value")

    if staged_root is None:
        raise ProjectStoragePathError("staged source root is required")
    raw = str(source).replace("\\", "/")
    source_path = Path(raw)
    if source_path.is_absolute() or (len(raw) >= 2 and raw[1] == ":"):
        # Absolute sources are accepted only when they are already inside the
        # opaque trusted staging root.  A model cannot change that root.
        candidate = source_path
    else:
        parts = tuple(part for part in raw.split("/") if part)
        if not parts or any(part in {".", ".."} for part in parts):
            raise ProjectStoragePathError("staged source traversal is not allowed")
        candidate = staged_root.joinpath(*parts)
    _assert_no_reparse_components(candidate, root=staged_root)
    candidate = Path(os.path.realpath(os.path.abspath(candidate)))
    try:
        candidate.relative_to(staged_root)
    except ValueError as exc:
        raise ProjectStoragePathError("staged source escaped trusted staging root") from exc
    if not candidate.is_file():
        raise ProjectStoragePathError("staged source must be a regular file")
    try:
        return candidate.read_bytes()
    except OSError as exc:
        raise ProjectStoragePublishError(f"failed to read staged source: {candidate}") from exc


def _coerce_diff(diff: ProjectStorageDiff | Mapping[str, Any]) -> ProjectStorageDiff:
    if isinstance(diff, ProjectStorageDiff):
        return diff
    if not isinstance(diff, Mapping):
        raise ProjectStoragePathError("diff must be a ProjectStorageDiff or structured mapping")
    # A model payload cannot inject authority fields.  Reject rather than
    # silently accepting ``staged_root``/``workspace_root`` from it.
    if any(key in diff for key in ("staged_root", "workspace_root", "root", "write_roots", "delete_roots")):
        raise ProjectStoragePermissionError("diff cannot carry filesystem authority")
    return ProjectStorageDiff(
        writes=diff.get("writes", diff.get("write", {})) or {},
        deletes=diff.get("deletes", diff.get("delete", ())) or (),
        operation_id=diff.get("operation_id", diff.get("idempotency_key")),
    )


def _diff_digest(writes: Sequence[_PreparedWrite], deletes: Sequence[str]) -> str:
    hasher = hashlib.sha256()
    for item in writes:
        hasher.update(b"w\0")
        hasher.update(item.relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(item.content)
    for relative in deletes:
        hasher.update(b"d\0")
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


class ProjectStoragePublisher:
    """Publish one trusted staged Project diff transactionally."""

    def __init__(
        self,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        staged_root: str | os.PathLike[str] | None = None,
        managed_paths: Iterable[str] = (),
        app_service: Any | None = None,
    ) -> None:
        # Constructor arguments are service-side configuration, not model
        # payload.  They are canonicalised once and never replaced per call.
        configured = workspace_root if workspace_root is not None else get_workspaces_root()
        self.workspace_root = _assert_safe_root(Path(configured))
        self.staged_root = (
            _assert_safe_root(Path(staged_root)) if staged_root is not None else None
        )
        self.managed_paths = frozenset(
            _path_key(_normalise_relative_path(path)) for path in managed_paths
        )
        self.app_service = app_service

    @staticmethod
    def _result_from_payload(
        payload: Mapping[str, Any],
        *,
        project_id: UUID,
        principal_id: UUID,
        operation_id: str,
    ) -> ProjectStoragePublishResult:
        return ProjectStoragePublishResult(
            project_id=project_id,
            principal_id=principal_id,
            operation_id=operation_id,
            writes=tuple(str(item) for item in payload.get("writes", ()) or ()),
            deletes=tuple(str(item) for item in payload.get("deletes", ()) or ()),
            changed=bool(payload.get("changed")),
            idempotent=True,
            storage_used_mb=float(payload.get("storage_used_mb") or 0.0),
            total_bytes=int(payload.get("total_bytes") or 0),
        )

    async def _recover_incomplete_journals(
        self,
        session: Any,
        *,
        project_id: UUID,
    ) -> None:
        """Lazily reconcile crash-persistent journals under the Project lock."""

        journal_base = self.workspace_root / ".project-publish-journal"
        if not journal_base.exists():
            return
        _assert_no_reparse_components(journal_base, root=self.workspace_root)
        children = list(journal_base.iterdir())
        if len(children) > 4096:
            raise ProjectStorageRollbackError(
                "too many incomplete Project publication journals"
            )
        scalar = getattr(session, "scalar", None)
        delete = getattr(session, "delete", None)
        flush = getattr(session, "flush", None)
        if not callable(scalar) or not callable(delete) or not callable(flush):
            if children:
                raise ProjectStorageRollbackError(
                    "durable Project publication recovery requires a database session"
                )
            return
        for child in children:
            if _is_reparse_or_symlink(child) or not child.is_dir():
                raise ProjectStorageRollbackError(
                    "Project publication journal storage contains an unsafe entry"
                )
            journal = _FilesystemJournal.load(
                child,
                workspace_root=self.workspace_root,
            )
            if journal.project_id != str(project_id):
                continue
            row = None
            if journal.operation_id:
                row = await scalar(
                    select(ProjectStorageOperation)
                    .where(
                        ProjectStorageOperation.project_id == UUID(journal.project_id),
                        ProjectStorageOperation.principal_id
                        == UUID(journal.principal_id),
                        ProjectStorageOperation.operation_id == journal.operation_id,
                    )
                    .with_for_update()
                )
            if row is not None and row.diff_sha256 != journal.diff_sha256:
                raise ProjectStorageRollbackError(
                    "Project publication journal does not match durable operation"
                )
            if row is not None and row.state == "committed":
                # Filesystem publication completed and the durable result won;
                # only cleanup was interrupted.
                journal.cleanup()
                continue
            # No committed outcome exists, so the pre-mutation snapshot wins.
            journal.restore(workspace_root=self.workspace_root)
            journal.cleanup()
            if row is not None:
                maybe_awaitable = delete(row)
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
                await flush()

    async def _durable_operation(
        self,
        session: Any,
        *,
        project_id: UUID,
        principal_id: UUID,
        operation_id: str | None,
        digest: str,
    ) -> tuple[ProjectStorageOperation | None, ProjectStoragePublishResult | None]:
        if operation_id is None:
            return None, None
        scalar = getattr(session, "scalar", None)
        add = getattr(session, "add", None)
        flush = getattr(session, "flush", None)
        if not callable(scalar) or not callable(add) or not callable(flush):
            # Lightweight service fakes retain the bounded in-process cache;
            # production AsyncSession always uses the durable ledger below.
            return None, None
        row = await scalar(
            select(ProjectStorageOperation)
            .where(
                ProjectStorageOperation.project_id == project_id,
                ProjectStorageOperation.principal_id == principal_id,
                ProjectStorageOperation.operation_id == operation_id,
            )
            .with_for_update()
        )
        if row is not None:
            if row.diff_sha256 != digest:
                raise ProjectStorageIdempotencyError(
                    "operation_id was reused for another diff"
                )
            if row.state == "committed":
                return row, self._result_from_payload(
                    row.result_json if isinstance(row.result_json, Mapping) else {},
                    project_id=project_id,
                    principal_id=principal_id,
                    operation_id=operation_id,
                )
            # The Project-wide OS lock proves no publisher is still using this
            # row.  Crash journals were reconciled immediately before this
            # lookup, so a remaining prepared/failed row has no filesystem
            # outcome and can be replaced for a same-digest retry.
            delete = getattr(session, "delete", None)
            if not callable(delete):
                raise ProjectStorageIdempotencyError(
                    "an incomplete durable Project publication requires recovery"
                )
            maybe_awaitable = delete(row)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
            await flush()
        row = ProjectStorageOperation(
            id=uuid4(),
            project_id=project_id,
            principal_id=principal_id,
            operation_id=operation_id,
            diff_sha256=digest,
            state="prepared",
            result_json={},
        )
        add(row)
        await flush()
        return row, None

    async def publish(
        self,
        session: Any,
        principal: Any,
        project_id: UUID | str,
        diff: ProjectStorageDiff | Mapping[str, Any],
        *,
        capability: ProjectStorageCapability | None = None,
        operation_id: str | None = None,
    ) -> ProjectStoragePublishResult:
        principal_id = _normalise_principal_id(principal)
        project_uuid = _coerce_uuid(project_id, "project_id")
        structured = _coerce_diff(diff)
        effective_operation_id = str(operation_id or structured.operation_id or "").strip() or None
        if effective_operation_id is not None and len(effective_operation_id) > 256:
            raise ProjectStorageIdempotencyError("operation_id is too long")
        if effective_operation_id is not None and any(
            character in effective_operation_id for character in ("/", "\\", "\x00", "\r", "\n")
        ):
            raise ProjectStorageIdempotencyError("operation_id contains invalid characters")

        if capability is not None:
            if not isinstance(capability, ProjectStorageCapability):
                raise ProjectStoragePermissionError("invalid Project storage capability")
            if capability.principal_id != principal_id or capability.project_id != project_uuid:
                raise ProjectStoragePermissionError("Project storage capability identity mismatch")
            if capability._token is not _CAPABILITY_TOKEN:
                raise ProjectStoragePermissionError("invalid Project storage capability")
            staged_root = capability.staged_root
            allow_write = capability.allow_write
            allow_delete = capability.allow_delete
        else:
            staged_root = self.staged_root
            allow_write = True
            allow_delete = False
        if staged_root is not None:
            staged_root = _assert_safe_root(staged_root)

        # Resolve the generated storage namespace only after acquiring the
        # lock.  ProjectRepository's helper is canonical and not caller data.
        lock = project_operation_lock(project_uuid, workspace_root=self.workspace_root)
        await lock.acquire()
        try:
            project = await ProjectRepository.get_by_id_for_update(session, project_uuid)
            if project is None or getattr(project, "deleted_at", None) is not None:
                raise ProjectStoragePermissionError("Project not found")

            acl = await self._recheck_acl(session, project_uuid, principal_id)
            if not acl["read"]:
                raise ProjectStoragePermissionError("Project read permission denied")
            if structured.writes and (not acl["write"] or not allow_write):
                raise ProjectStoragePermissionError("Project write permission denied")
            if structured.deletes and (not acl["delete"] or not allow_delete):
                raise ProjectStoragePermissionError("Project delete permission denied")

            storage_relative = await ProjectRepository.get_storage_path(project_uuid)
            project_root = _canonical_project_root(
                self.workspace_root,
                storage_relative,
                project_id=project_uuid,
            )
            await self._recover_incomplete_journals(
                session,
                project_id=project_uuid,
            )
            known_managed = set(self.managed_paths)
            metadata = getattr(project, "project_metadata", None)
            for candidate in _metadata_paths(metadata):
                normalized = _normalise_reference_path(candidate, project_uuid)
                if normalized:
                    known_managed.add(_path_key(normalized))
            known_managed.update(_path_key(item) for item in await _load_known_reference_paths(session, project_uuid))
            canonical_managed = set(known_managed)
            for managed in tuple(known_managed):
                try:
                    managed_destination = _canonical_destination(
                        project_root,
                        managed,
                    )
                    canonical_managed.add(
                        _canonical_relative_key(
                            project_root,
                            managed_destination,
                        )
                    )
                except ProjectStoragePathError as exc:
                    raise ProjectStorageManagedPathError(
                        f"durable managed Project path is unsafe: {managed}"
                    ) from exc
            known_managed = canonical_managed

            prepared_writes, normalized_deletes = self._prepare_diff(
                structured,
                staged_root=staged_root,
                project_root=project_root,
                known_managed=known_managed,
            )
            write_paths = [item.relative for item in prepared_writes]
            for index, left in enumerate(write_paths):
                for right in write_paths[index + 1 :]:
                    if left.startswith(f"{right}/") or right.startswith(f"{left}/"):
                        raise ProjectStoragePathError(
                            "a diff cannot write both a path and its descendant"
                        )
            for written in write_paths:
                for deleted in normalized_deletes:
                    if (
                        written == deleted
                        or written.startswith(f"{deleted}/")
                        or deleted.startswith(f"{written}/")
                    ):
                        raise ProjectStoragePathError(
                            "write and delete paths cannot overlap in one diff"
                        )
            digest = _diff_digest(prepared_writes, normalized_deletes)

            durable_operation, durable_result = await self._durable_operation(
                session,
                project_id=project_uuid,
                principal_id=principal_id,
                operation_id=effective_operation_id,
                digest=digest,
            )
            if durable_result is not None:
                return durable_result

            # Idempotency is checked after the row lock and ACL recheck so a
            # replay never bypasses a revoked principal.
            if effective_operation_id is not None:
                key = (project_uuid, principal_id, effective_operation_id)
                with _IDEMPOTENCY_LOCK:
                    previous = _COMPLETED_OPERATIONS.get(key)
                if previous is not None:
                    previous_digest, previous_result = previous
                    if previous_digest != digest:
                        raise ProjectStorageIdempotencyError("operation_id was reused for another diff")
                    return ProjectStoragePublishResult(
                        project_id=project_uuid,
                        principal_id=principal_id,
                        operation_id=previous_result.operation_id,
                        writes=previous_result.writes,
                        deletes=previous_result.deletes,
                        changed=previous_result.changed,
                        idempotent=True,
                        storage_used_mb=previous_result.storage_used_mb,
                        total_bytes=previous_result.total_bytes,
                    )

            return await self._publish_locked(
                session=session,
                project=project,
                principal_id=principal_id,
                project_uuid=project_uuid,
                project_root=project_root,
                prepared_writes=prepared_writes,
                normalized_deletes=normalized_deletes,
                operation_id=effective_operation_id,
                digest=digest,
                quota_mb=getattr(project, "storage_quota_mb", None),
                durable_operation=durable_operation,
            )
        finally:
            lock.release()

    async def publish_diff(self, *args: Any, **kwargs: Any) -> ProjectStoragePublishResult:
        """Compatibility alias for future sandbox diff publication."""

        return await self.publish(*args, **kwargs)

    async def publish_project_diff(self, *args: Any, **kwargs: Any) -> ProjectStoragePublishResult:
        return await self.publish(*args, **kwargs)

    async def _recheck_acl(self, session: Any, project_id: UUID, principal_id: UUID) -> dict[str, bool]:
        permissions: dict[str, bool] = {}
        for permission in ("read", "write", "delete"):
            try:
                permissions[permission] = bool(
                    await ProjectRepository.has_permission(
                        session,
                        project_id=project_id,
                        user_id=principal_id,
                        permission=permission,
                    )
                )
            except TypeError:
                # Small repository fakes often expose positional-only args.
                permissions[permission] = bool(
                    await ProjectRepository.has_permission(session, project_id, principal_id, permission)
                )
        # Optional AppService adapter lets an integration use its canonical
        # project_access/project_write_access policy while the repository
        # remains the source of delete authority.
        if self.app_service is not None:
            try:
                access = await self.app_service.project_access(
                    session, project_id=project_id, user_id=principal_id
                )
                permissions["read"] = permissions["read"] and bool(access)
            except (AttributeError, TypeError):
                pass
            if permissions["write"]:
                try:
                    write_access = await self.app_service.project_write_access(
                        session, project_id=project_id, user_id=principal_id
                    )
                    permissions["write"] = bool(write_access)
                except (AttributeError, TypeError):
                    pass
        return permissions

    def _prepare_diff(
        self,
        diff: ProjectStorageDiff,
        *,
        staged_root: Path | None,
        project_root: Path,
        known_managed: set[str],
    ) -> tuple[list[_PreparedWrite], list[str]]:
        if len(diff.writes) + len(diff.deletes) > _MAX_DIFF_ENTRIES:
            raise ProjectStoragePathError(
                f"Project publication exceeds {_MAX_DIFF_ENTRIES} diff entries"
            )
        writes: list[_PreparedWrite] = []
        seen_writes: set[str] = set()
        total_write_bytes = 0
        for raw_relative, raw_value in diff.writes.items():
            relative = _normalise_relative_path(raw_relative)
            key = _path_key(relative)
            if key in seen_writes:
                raise ProjectStoragePathError(f"duplicate write path: {relative}")
            if _conflicts_with_managed_path(key, known_managed):
                raise ProjectStorageManagedPathError(f"managed/reference path cannot be written: {relative}")
            destination = _canonical_destination(project_root, relative)
            canonical_key = _canonical_relative_key(project_root, destination)
            if _conflicts_with_managed_path(canonical_key, known_managed):
                raise ProjectStorageManagedPathError(
                    f"managed/reference path cannot be written: {relative}"
                )
            if destination.exists() and destination.is_dir():
                raise ProjectStoragePathError(f"cannot overwrite Project directory: {relative}")
            content = _coerce_staged_bytes(raw_value, staged_root=staged_root)
            total_write_bytes += len(content)
            if total_write_bytes > _MAX_DIFF_BYTES:
                raise ProjectStorageQuotaError(
                    f"Project publication diff exceeds {_MAX_DIFF_BYTES} bytes"
                )
            # Re-check destination after source read to catch a concurrently
            # introduced link/reparse entry before any mutation.
            _assert_no_reparse_components(destination, root=project_root)
            writes.append(_PreparedWrite(relative, content))
            seen_writes.add(key)

        deletes: list[str] = []
        seen_deletes: set[str] = set()
        for raw_relative in diff.deletes:
            relative = _normalise_relative_path(raw_relative)
            key = _path_key(relative)
            if key in seen_deletes:
                # Duplicate deletes are naturally idempotent but a duplicate
                # path usually indicates a malformed diff; fail closed.
                raise ProjectStoragePathError(f"duplicate delete path: {relative}")
            if _conflicts_with_managed_path(key, known_managed):
                raise ProjectStorageManagedPathError(f"managed/reference path cannot be deleted: {relative}")
            destination = _canonical_destination(project_root, relative)
            canonical_key = _canonical_relative_key(project_root, destination)
            if _conflicts_with_managed_path(canonical_key, known_managed):
                raise ProjectStorageManagedPathError(
                    f"managed/reference path cannot be deleted: {relative}"
                )
            if destination.exists() and destination.is_dir():
                _assert_tree_has_no_links(destination)
            seen_deletes.add(key)
            deletes.append(relative)
        return writes, deletes

    async def _publish_locked(
        self,
        *,
        session: Any,
        project: Any,
        principal_id: UUID,
        project_uuid: UUID,
        project_root: Path,
        prepared_writes: Sequence[_PreparedWrite],
        normalized_deletes: Sequence[str],
        operation_id: str | None,
        digest: str,
        quota_mb: Any,
        durable_operation: ProjectStorageOperation | None,
    ) -> ProjectStoragePublishResult:
        before_bytes = _strict_storage_usage(project_root)
        projected_bytes = before_bytes
        targets: dict[Path, tuple[str, bytes | None]] = {}
        for item in prepared_writes:
            target = _canonical_destination(project_root, item.relative)
            previous_size = target.stat().st_size if target.exists() else 0
            projected_bytes += len(item.content) - previous_size
            targets[target] = ("write", item.content)
        for relative in normalized_deletes:
            target = _canonical_destination(project_root, relative)
            if target.exists():
                if target.is_dir():
                    projected_bytes -= _strict_storage_usage(target)
                else:
                    projected_bytes -= target.stat().st_size
            targets[target] = ("delete", None)
        try:
            # Canonical Project migrations backfill 1000 MiB and make this
            # column non-null.  Fail closed to the same bounded default for a
            # legacy/drifted row instead of interpreting NULL as unlimited.
            quota = int(quota_mb) if quota_mb is not None else 1000
        except (TypeError, ValueError) as exc:
            raise ProjectStorageQuotaError("Project storage quota is invalid") from exc
        if quota is not None and quota < 0:
            raise ProjectStorageQuotaError("Project storage quota cannot be negative")
        if projected_bytes < 0:
            projected_bytes = 0
        if quota is not None and projected_bytes > quota * 1024 * 1024:
            raise ProjectStorageQuotaError(
                f"Project storage quota exceeded: {projected_bytes} > {quota * 1024 * 1024} bytes"
            )

        operation_marker = operation_id or f"publish-{uuid4().hex}"
        journal = _FilesystemJournal.create(
            self.workspace_root,
            operation_marker,
            project_id=project_uuid,
            principal_id=principal_id,
            diff_sha256=digest,
        )
        changed_writes: list[str] = []
        changed_deletes: list[str] = []
        changed = False
        try:
            for item in prepared_writes:
                target = _canonical_destination(project_root, item.relative)
                if target.exists() and target.is_file() and target.read_bytes() == item.content:
                    continue
                journal.backup_target(target, workspace_root=self.workspace_root)
                _ensure_parent_directory(target.parent, root=project_root, journal=journal)
                temporary: Path | None = None
                try:
                    fd, name = tempfile.mkstemp(prefix=".project-publish-", dir=target.parent)
                    temporary = Path(name)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(item.content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    _assert_no_reparse_components(target.parent, root=project_root)
                    os.replace(temporary, target)
                    temporary = None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                changed_writes.append(item.relative)
                changed = True

            for relative in normalized_deletes:
                target = _canonical_destination(project_root, relative)
                if not target.exists():
                    continue
                journal.backup_target(target, workspace_root=self.workspace_root)
                if target.is_dir():
                    _assert_tree_has_no_links(target)
                    shutil.rmtree(target)
                else:
                    target.unlink()
                changed_deletes.append(relative)
                changed = True

            final_bytes = _strict_storage_usage(project_root)
            if quota is not None and final_bytes > quota * 1024 * 1024:
                raise ProjectStorageQuotaError("Project storage quota exceeded after filesystem publication")
            used_mb = final_bytes / (1024 * 1024)
            project.storage_used_mb = used_mb
            if durable_operation is not None:
                durable_operation.state = "committed"
                durable_operation.result_json = {
                    "writes": list(changed_writes),
                    "deletes": list(changed_deletes),
                    "changed": changed,
                    "storage_used_mb": used_mb,
                    "total_bytes": final_bytes,
                }
            await session.commit()
        except BaseException as exc:
            try:
                await session.rollback()
            except BaseException:
                logger.exception("Project storage DB rollback failed")
            if durable_operation is not None and callable(
                getattr(session, "scalar", None)
            ):
                try:
                    persisted = await session.scalar(
                        select(ProjectStorageOperation).where(
                            ProjectStorageOperation.project_id == project_uuid,
                            ProjectStorageOperation.principal_id == principal_id,
                            ProjectStorageOperation.operation_id == operation_id,
                        )
                    )
                except BaseException as lookup_exc:
                    # The DB may have committed even though the connection
                    # reported an error.  Never guess and roll filesystem back
                    # against an unknown durable state; retain the journal for
                    # operator/retry recovery.
                    raise ProjectStorageRollbackError(
                        "Project publication commit outcome is unknown; "
                        f"journal retained at {journal.root}"
                    ) from lookup_exc
                if persisted is not None and persisted.state == "committed":
                    journal.cleanup()
                    return self._result_from_payload(
                        persisted.result_json
                        if isinstance(persisted.result_json, Mapping)
                        else {},
                        project_id=project_uuid,
                        principal_id=principal_id,
                        operation_id=str(operation_id),
                    )
            try:
                journal.restore(workspace_root=self.workspace_root)
            except BaseException as restore_exc:
                journal.cleanup()
                raise ProjectStorageRollbackError("Project storage publication rollback failed") from restore_exc
            journal.cleanup()
            raise exc
        else:
            journal.cleanup()

        result = ProjectStoragePublishResult(
            project_id=project_uuid,
            principal_id=principal_id,
            operation_id=operation_id,
            writes=tuple(changed_writes),
            deletes=tuple(changed_deletes),
            changed=changed,
            idempotent=False,
            storage_used_mb=used_mb,
            total_bytes=final_bytes,
        )
        if operation_id is not None:
            key = (project_uuid, principal_id, operation_id)
            with _IDEMPOTENCY_LOCK:
                if len(_COMPLETED_OPERATIONS) >= _MAX_COMPLETED_OPERATIONS:
                    # FIFO ordering is unnecessary for safety; dropping one
                    # arbitrary completed key keeps memory bounded.
                    _COMPLETED_OPERATIONS.pop(next(iter(_COMPLETED_OPERATIONS)))
                _COMPLETED_OPERATIONS[key] = (digest, result)
        return result


def _assert_tree_has_no_links(root: Path) -> None:
    _assert_no_reparse_components(root)
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in tuple(directory_names) + tuple(file_names):
            child = current_path / name
            if _is_reparse_or_symlink(child):
                raise ProjectStoragePathError(f"tree contains link/reparse entry: {child}")
        directory_names[:] = [name for name in directory_names if not _is_reparse_or_symlink(current_path / name)]


async def publish_project_storage_diff(
    session: Any,
    principal: Any,
    project_id: UUID | str,
    diff: ProjectStorageDiff | Mapping[str, Any],
    *,
    capability: ProjectStorageCapability | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    staged_root: str | os.PathLike[str] | None = None,
    managed_paths: Iterable[str] = (),
    operation_id: str | None = None,
) -> ProjectStoragePublishResult:
    """Functional facade for future sandbox diff publication integrations."""

    publisher = ProjectStoragePublisher(
        workspace_root=workspace_root,
        staged_root=staged_root,
        managed_paths=managed_paths,
    )
    return await publisher.publish(
        session,
        principal,
        project_id,
        diff,
        capability=capability,
        operation_id=operation_id,
    )


publish_project_diff = publish_project_storage_diff


__all__ = [
    "ProjectStoragePublishError",
    "ProjectStoragePathError",
    "ProjectStoragePermissionError",
    "ProjectStorageQuotaError",
    "ProjectStorageManagedPathError",
    "ProjectStorageIdempotencyError",
    "ProjectStorageRollbackError",
    "ProjectStorageCapability",
    "TrustedProjectStorageCapability",
    "ProjectPublishCapability",
    "issue_project_storage_capability",
    "StagedFile",
    "StagedContent",
    "ProjectStorageDiff",
    "StagedProjectDiff",
    "ProjectPublishDiff",
    "ProjectStoragePublishResult",
    "ProjectStoragePublisher",
    "publish_project_storage_diff",
    "publish_project_diff",
]
