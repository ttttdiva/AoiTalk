"""Run-owned file storage for App Action inputs and outputs.

Action input files are uploaded before a run exists and are addressed only by
an opaque ``file_id``.  Once an execution owner creates a run the input bytes
are copied into a run namespace and the copy becomes the worker's immutable
input.  Output files are created directly below the run namespace.  Neither
kind of file is stored in an App source workspace or in the legacy
``WorkspaceFileEntry`` inventory.

The service deliberately keeps its durable index on disk.  A file id is
random and its blob path is derived from the id and (for outputs) the run id;
the JSON sidecar is treated as untrusted metadata and is checked against the
derived path, owner/app/run scope, expiry, size and SHA-256 before bytes are
read.  This keeps the service usable while the execution team's durable DB
model is being finalized.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import hashlib
import inspect
import json
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Iterable, Mapping
from uuid import UUID

from .app_operation_lock import AppOperationLock
from .app_storage import _is_storage_link_or_reparse, ensure_workspaces_root, get_workspaces_root


FILE_PROTOCOL_VERSION = "1"
FILE_ROOT_ENV = "AOITALK_ACTION_FILE_ROOT"
INPUT_FILES_ENV = "AOITALK_ACTION_INPUT_FILES"
FILE_PROTOCOL_ENV = "AOITALK_ACTION_FILE_PROTOCOL_VERSION"

# Keep uploads bounded even when a caller omits Content-Length.  The value is
# intentionally independent from App source upload limits: these bytes are
# task data, not executable source.
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_INPUT_TTL_SECONDS = 60 * 60
DEFAULT_OUTPUT_TTL_SECONDS = 7 * 24 * 60 * 60
CHUNK_SIZE = 1024 * 1024

_FILE_ID_RE = re.compile(r"^af_[A-Za-z0-9_-]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

_FILE_METADATA_KEYS = {
    "file_id",
    "filename",
    "name",
    "content_type",
    "size_bytes",
    "sha256",
    "kind",
}
_FORBIDDEN_FILE_REFERENCE_KEYS = {
    "path",
    "url",
    "uri",
    "storage_path",
    "local_path",
    "absolute_path",
}


class AppActionFileError(RuntimeError):
    """Expected action-file failure translated by the HTTP route."""

    status_code = 400
    code = "app_action_file_error"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = int(status_code)
        if code is not None:
            self.code = str(code)


class AppActionFileValidationError(AppActionFileError):
    status_code = 422
    code = "app_action_file_validation_error"


class AppActionFileForbidden(AppActionFileError):
    status_code = 403
    code = "app_action_file_forbidden"


class AppActionFileNotFound(AppActionFileError):
    status_code = 404
    code = "app_action_file_not_found"


class AppActionFileConflict(AppActionFileError):
    status_code = 409
    code = "app_action_file_conflict"


class AppActionFileUnavailable(AppActionFileError):
    status_code = 503
    code = "app_action_file_unavailable"


def _as_uuid(value: UUID | str | None, label: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AppActionFileValidationError(f"{label} は UUID で指定してください", code="invalid_" + label) from exc


def _as_uuid_value(value: UUID | str | None, label: str, *, required: bool = True) -> UUID | None:
    normalized = _as_uuid(value, label, required=required)
    return UUID(normalized) if normalized else None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise AppActionFileUnavailable("file metadataの期限が不正です", code="file_metadata_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_filename(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 255 or "\x00" in raw:
        raise AppActionFileValidationError("filenameが不正です", code="invalid_filename")
    # A filename is metadata, not a path.  Requiring one component avoids
    # Windows/Posix ambiguity and makes Content-Disposition safe.
    if "/" in raw or "\\" in raw or raw in {".", ".."} or _DRIVE_RE.match(raw):
        raise AppActionFileForbidden("filenameにpath traversalがあります", code="filename_path_traversal")
    if any(ord(char) < 32 for char in raw):
        raise AppActionFileValidationError("filenameに制御文字があります", code="invalid_filename")
    return raw


def _safe_content_type(value: str | None, filename: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if len(raw) > 255 or "\x00" in raw or "\r" in raw or "\n" in raw:
        raise AppActionFileValidationError("content_typeが不正です", code="invalid_content_type")
    return raw


def _file_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _FILE_ID_RE.fullmatch(normalized):
        raise AppActionFileValidationError("file_idはopaque file idで指定してください", code="invalid_file_id")
    return normalized


def _safe_relative(value: str) -> str:
    raw = str(value or "").replace("\\", "/")
    if not raw or raw.startswith("/") or _DRIVE_RE.match(raw):
        raise AppActionFileForbidden("storage pathが不正です", code="storage_path_invalid")
    parts = tuple(part for part in PurePosixPath(raw).parts if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise AppActionFileForbidden("storage pathにpath traversalがあります", code="storage_path_traversal")
    return "/".join(parts)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AppActionFileValidationError("input file metadataはJSONで指定してください", code="invalid_file_metadata") from exc


@dataclass(frozen=True)
class ActionFileMetadata:
    """Typed internal metadata projection; no local path is exposed."""

    file_id: str
    kind: str
    app_id: str
    owner_user_id: str
    project_id: str | None
    action_id: str | None
    run_id: str | None
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: str
    expires_at: str | None
    download_url: str | None = None
    source_ref: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActionFileMetadata":
        required = ("file_id", "kind", "app_id", "owner_user_id", "filename", "content_type", "size_bytes", "sha256", "created_at")
        if any(key not in value for key in required):
            raise AppActionFileUnavailable("file metadataが不足しています", code="file_metadata_invalid")
        file_id = _file_id(value.get("file_id"))
        kind = str(value.get("kind") or "")
        if kind not in {"input", "output"}:
            raise AppActionFileUnavailable("file metadataのkindが不正です", code="file_metadata_invalid")
        app_id = _as_uuid(value.get("app_id"), "app_id") or ""
        owner = _as_uuid(value.get("owner_user_id"), "owner_user_id") or ""
        project = _as_uuid(value.get("project_id"), "project_id", required=False)
        run = _as_uuid(value.get("run_id"), "run_id", required=False)
        action = str(value.get("action_id") or "") or None
        filename = _safe_filename(value.get("filename"))
        content_type = _safe_content_type(value.get("content_type"), filename)
        try:
            size = int(value.get("size_bytes"))
        except (TypeError, ValueError) as exc:
            raise AppActionFileUnavailable("file metadataのsizeが不正です", code="file_metadata_invalid") from exc
        digest = str(value.get("sha256") or "").lower()
        if size < 0 or not _SHA256_RE.fullmatch(digest):
            raise AppActionFileUnavailable("file metadataのintegrity値が不正です", code="file_metadata_invalid")
        created = str(value.get("created_at") or "")
        if _parse_time(created) is None:
            raise AppActionFileUnavailable("file metadataのcreated_atが不正です", code="file_metadata_invalid")
        expires = _utc_iso(_parse_time(value.get("expires_at")))
        if kind == "input" and run is not None:
            raise AppActionFileUnavailable("input fileにrun_idがあります", code="file_metadata_invalid")
        if kind == "output" and run is None:
            raise AppActionFileUnavailable("output fileにrun_idがありません", code="file_metadata_invalid")
        return cls(
            file_id=file_id,
            kind=kind,
            app_id=app_id,
            owner_user_id=owner,
            project_id=project,
            action_id=action,
            run_id=run,
            filename=filename,
            content_type=content_type,
            size_bytes=size,
            sha256=digest,
            created_at=_utc_iso(_parse_time(created)) or created,
            expires_at=expires,
            download_url=str(value.get("download_url") or "") or None,
            source_ref=_safe_relative(value.get("source_ref")) if value.get("source_ref") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "file_id": self.file_id,
            "kind": self.kind,
            "app_id": self.app_id,
            "owner_user_id": self.owner_user_id,
            "project_id": self.project_id,
            "action_id": self.action_id,
            "run_id": self.run_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "download_url": self.download_url,
        }
        if self.source_ref:
            payload["source_ref"] = self.source_ref
        return payload


@dataclass(frozen=True)
class ActionFileReference:
    """A validated input reference suitable for immutable run input."""

    metadata: ActionFileMetadata
    relative_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.metadata.file_id,
            "filename": self.metadata.filename,
            "content_type": self.metadata.content_type,
            "size_bytes": self.metadata.size_bytes,
            "sha256": self.metadata.sha256,
        }


def _maybe_await(value: Any) -> Any:
    return value if inspect.isawaitable(value) else value


class AppActionFileService:
    """Own the task file namespace and the input/output integrity boundary."""

    def __init__(
        self,
        workspace_root: str | os.PathLike[str] | None = None,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        input_ttl_seconds: int = DEFAULT_INPUT_TTL_SECONDS,
        output_ttl_seconds: int = DEFAULT_OUTPUT_TTL_SECONDS,
    ) -> None:
        if int(max_file_bytes) <= 0:
            raise ValueError("max_file_bytes must be positive")
        configured_root = workspace_root if workspace_root is not None else os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
        configured_path = Path(configured_root).expanduser()
        if _is_storage_link_or_reparse(configured_path):
            raise AppActionFileUnavailable("workspace rootのsymlinkは利用できません", code="storage_root_invalid")
        self.workspace_root = get_workspaces_root(workspace_root)
        self.root = self.workspace_root / "_app_action_files"
        self.max_file_bytes = int(max_file_bytes)
        self.input_ttl_seconds = max(1, int(input_ttl_seconds))
        self.output_ttl_seconds = max(1, int(output_ttl_seconds))
        self._validate_roots()

    def _validate_roots(self) -> None:
        self._assert_safe_root(self.workspace_root)
        self._assert_storage_directory(self.root, "Action file rootが不正です")
        try:
            self.root.resolve().relative_to(self.workspace_root)
        except ValueError as exc:
            raise AppActionFileUnavailable("Action file rootが不正です", code="storage_root_invalid") from exc
        for name in ("inputs", "runs", ".staging"):
            self._assert_storage_directory(self.root / name, "Action file namespaceが不正です")

    @staticmethod
    def _assert_storage_directory(path: Path, message: str) -> None:
        if _is_storage_link_or_reparse(path) or (path.exists() and not path.is_dir()):
            raise AppActionFileUnavailable(message, code="storage_root_invalid")

    def _ensure_root(self) -> None:
        self._validate_roots()
        ensure_workspaces_root(self.workspace_root)
        self.root.mkdir(exist_ok=True)
        self._validate_roots()

    def _ensure_namespace(self, name: str) -> Path:
        if name not in {"inputs", "runs", ".staging"}:
            raise AppActionFileValidationError("file namespaceが不正です", code="storage_path_invalid")
        self._ensure_root()
        path = self.root / name
        path.mkdir(exist_ok=True)
        self._validate_roots()
        return path

    def _ensure_run_subdir(self, run_id: UUID | str, name: str) -> Path:
        self._run_subdir(run_id, name)
        self._ensure_namespace("runs")
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(exist_ok=True)
        path = self._run_subdir(run_id, name)
        path.mkdir(exist_ok=True)
        return self._run_subdir(run_id, name)

    @staticmethod
    def _assert_safe_root(root: Path) -> None:
        if _is_storage_link_or_reparse(root) or (root.exists() and not root.is_dir()):
            raise AppActionFileUnavailable("workspace rootのsymlinkは利用できません", code="storage_root_invalid")

    def _under_root(self, target: Path, *, allow_root: bool = True) -> Path:
        self._validate_roots()
        root = self.root.resolve()
        resolved = target.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise AppActionFileForbidden("Action fileが許可root外です", code="storage_path_escape") from exc
        if not allow_root and not relative.parts:
            raise AppActionFileForbidden("Action file root自体は対象にできません", code="storage_path_invalid")
        return resolved

    def _run_dir(self, run_id: UUID | str) -> Path:
        run = _as_uuid(run_id, "run_id") or ""
        target = self.root / "runs" / f"run_{run}"
        self._assert_storage_directory(target, "run file namespaceが不正です")
        return self._under_root(target)

    def _run_subdir(self, run_id: UUID | str, name: str) -> Path:
        if name not in {"inputs", "outputs"}:
            raise AppActionFileValidationError("run file namespaceが不正です", code="storage_path_invalid")
        path = self._run_dir(run_id) / name
        self._assert_storage_directory(path, "run file namespaceが不正です")
        return self._under_root(path)

    @staticmethod
    def _new_file_id() -> str:
        # 24 random bytes are encoded without path separators; IDs are never
        # derived from a filename, user, app or run identifier.
        return "af_" + secrets.token_urlsafe(24).replace("=", "")

    def _input_blob_path(self, file_id: str) -> Path:
        file_id = _file_id(file_id)
        return self._safe_storage_file(self.root / "inputs" / f"{file_id}.bin")

    def _input_metadata_path(self, file_id: str) -> Path:
        file_id = _file_id(file_id)
        return self._safe_storage_file(self.root / "inputs" / f"{file_id}.json")

    def _run_blob_path(self, run_id: UUID | str, file_id: str, *, kind: str) -> Path:
        file_id = _file_id(file_id)
        if kind not in {"input", "output"}:
            raise AppActionFileValidationError("file kindが不正です", code="invalid_file_kind")
        return self._safe_storage_file(self._run_subdir(run_id, "inputs" if kind == "input" else "outputs") / f"{file_id}.bin")

    def _run_metadata_path(self, run_id: UUID | str, file_id: str, *, kind: str) -> Path:
        file_id = _file_id(file_id)
        return self._safe_storage_file(self._run_subdir(run_id, "inputs" if kind == "input" else "outputs") / f"{file_id}.json")

    def _safe_storage_file(self, path: Path) -> Path:
        """Resolve one derived storage path without accepting file symlinks."""
        if _is_storage_link_or_reparse(path):
            raise AppActionFileForbidden("storage fileのsymlinkは許可されません", code="storage_file_symlink_forbidden")
        return self._under_root(path, allow_root=False)

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _copy_atomic(source: Path, destination: Path) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as source_stream:
                shutil.copyfileobj(source_stream, target, length=CHUNK_SIZE)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    async def _write_upload(self, upload: Any, destination: Path) -> tuple[int, str]:
        """Stream a FastAPI UploadFile or a sync/async file-like object."""
        fd, temporary = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=destination.parent)
        total = 0
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as target:
                while True:
                    reader = getattr(upload, "read", None)
                    if not callable(reader):
                        raise AppActionFileValidationError("uploadが読み込めません", code="invalid_upload")
                    chunk = reader(CHUNK_SIZE)
                    if inspect.isawaitable(chunk):
                        chunk = await chunk
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise AppActionFileValidationError("upload bytesが不正です", code="invalid_upload")
                    data = bytes(chunk)
                    total += len(data)
                    if total > self.max_file_bytes:
                        raise AppActionFileValidationError("file sizeが上限を超えています", code="file_too_large")
                    digest.update(data)
                    target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, destination)
            return total, digest.hexdigest()
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _metadata_payload(
        self,
        *,
        file_id: str,
        kind: str,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None,
        action_id: str | None,
        run_id: UUID | str | None,
        filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        expires_at: datetime | None,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"input", "output"}:
            raise AppActionFileValidationError("file kindが不正です", code="invalid_file_kind")
        payload = {
            "file_id": _file_id(file_id),
            "kind": kind,
            "app_id": _as_uuid(app_id, "app_id"),
            "owner_user_id": _as_uuid(owner_user_id, "owner_user_id"),
            "project_id": _as_uuid(project_id, "project_id", required=False),
            "action_id": str(action_id or "") or None,
            "run_id": _as_uuid(run_id, "run_id", required=False),
            "filename": _safe_filename(filename),
            "content_type": _safe_content_type(content_type, filename),
            "size_bytes": int(size_bytes),
            "sha256": str(sha256).lower(),
            "created_at": _utc_iso(_utc_now()),
            "expires_at": _utc_iso(expires_at),
        }
        if source_ref:
            payload["source_ref"] = _safe_relative(source_ref)
        return payload

    async def store_input_file(
        self,
        upload: Any,
        *,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        action_id: str,
        project_id: UUID | str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
        expected_sha256: str | None = None,
        ttl_seconds: int | None = None,
    ) -> ActionFileMetadata:
        """Persist a pre-run input upload and return opaque metadata."""
        app = _as_uuid(app_id, "app_id") or ""
        owner = _as_uuid(owner_user_id, "owner_user_id") or ""
        project = _as_uuid(project_id, "project_id", required=False)
        action = str(action_id or "").strip()
        if not action or len(action) > 255 or "\x00" in action:
            raise AppActionFileValidationError("action_idが不正です", code="invalid_action_id")
        safe_name = _safe_filename(filename if filename is not None else getattr(upload, "filename", None))
        safe_type = _safe_content_type(content_type if content_type is not None else getattr(upload, "content_type", None), safe_name)
        supplied_hash = str(expected_sha256 or "").strip().lower()
        if supplied_hash and not _SHA256_RE.fullmatch(supplied_hash):
            raise AppActionFileValidationError("expected_sha256が不正です", code="invalid_sha256")
        if not callable(getattr(upload, "read", None)):
            raise AppActionFileValidationError("uploadが読み込めません", code="invalid_upload")
        try:
            ttl = self.input_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise AppActionFileValidationError("input fileの有効期限が不正です", code="invalid_file_expiry") from exc
        if ttl <= 0 or ttl > 7 * 24 * 60 * 60:
            raise AppActionFileValidationError("input fileの有効期限が不正です", code="invalid_file_expiry")
        file_id = self._new_file_id()
        destination = self._input_blob_path(file_id)
        self._ensure_namespace("inputs")
        size, digest = await self._write_upload(upload, destination)
        if supplied_hash and supplied_hash != digest:
            destination.unlink(missing_ok=True)
            raise AppActionFileConflict("uploadのSHA-256が一致しません", code="file_hash_mismatch")
        metadata = self._metadata_payload(
            file_id=file_id,
            kind="input",
            app_id=app,
            owner_user_id=owner,
            project_id=project,
            action_id=action,
            run_id=None,
            filename=safe_name,
            content_type=safe_type,
            size_bytes=size,
            sha256=digest,
            expires_at=_utc_now() + timedelta(seconds=ttl),
        )
        try:
            ActionFileMetadata.from_dict(metadata)
            self._atomic_write(self._input_metadata_path(file_id), _canonical_json(metadata))
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return ActionFileMetadata.from_dict(metadata)

    # HTTP and execution adapters use both names while they converge on the
    # same operation.  Keep aliases explicit instead of making callers write
    # to the storage namespace themselves.
    upload_input_file = store_input_file

    def _read_metadata(self, path: Path, *, file_id: str) -> ActionFileMetadata:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AppActionFileNotFound("file metadataが見つかりません", code="file_not_found") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AppActionFileUnavailable("file metadataを読み込めません", code="file_metadata_unavailable") from exc
        if not isinstance(raw, Mapping) or str(raw.get("file_id") or "") != str(file_id):
            raise AppActionFileUnavailable("file metadataのfile_idが一致しません", code="file_metadata_invalid")
        return ActionFileMetadata.from_dict(raw)

    def _validate_scope(
        self,
        metadata: ActionFileMetadata,
        *,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None,
        action_id: str | None = None,
        run_id: UUID | str | None = None,
        kind: str | None = None,
        check_expiry: bool = True,
    ) -> None:
        app = _as_uuid(app_id, "app_id")
        owner = _as_uuid(owner_user_id, "owner_user_id")
        project = _as_uuid(project_id, "project_id", required=False)
        run = _as_uuid(run_id, "run_id", required=False)
        if metadata.app_id != app or metadata.owner_user_id != owner:
            raise AppActionFileForbidden("fileはowner/App scope外です", code="file_scope_forbidden")
        if metadata.project_id != project:
            raise AppActionFileForbidden("fileはProject scope外です", code="file_project_forbidden")
        if kind and metadata.kind != kind:
            raise AppActionFileForbidden("file kindがscopeと一致しません", code="file_kind_forbidden")
        if action_id is not None and metadata.action_id != str(action_id):
            raise AppActionFileConflict("fileはAction scope外です", code="file_action_mismatch")
        if run is not None and metadata.run_id != run:
            raise AppActionFileForbidden("fileはRun scope外です", code="file_run_forbidden")
        if check_expiry and metadata.expires_at:
            expires = _parse_time(metadata.expires_at)
            if expires is not None and expires <= _utc_now():
                raise AppActionFileConflict("input fileの有効期限が切れています", code="file_expired")

    def _verify_blob_metadata(self, metadata: ActionFileMetadata, path: Path) -> Path:
        path = self._under_root(path, allow_root=False)
        if path.is_symlink() or not path.is_file():
            raise AppActionFileNotFound("file bytesが見つかりません", code="file_bytes_not_found")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise AppActionFileUnavailable("file sizeを確認できません", code="file_integrity_unavailable") from exc
        if metadata.size_bytes > self.max_file_bytes:
            raise AppActionFileUnavailable("file sizeが上限を超えています", code="file_size_limit_exceeded")
        if size != metadata.size_bytes:
            raise AppActionFileUnavailable("file size検証に失敗しました", code="file_size_mismatch")
        return path

    def _verify_blob(self, metadata: ActionFileMetadata, path: Path) -> Path:
        path = self._verify_blob_metadata(metadata, path)
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise AppActionFileUnavailable("file hashを確認できません", code="file_integrity_unavailable") from exc
        if digest.hexdigest().casefold() != metadata.sha256.casefold():
            raise AppActionFileUnavailable("file hash検証に失敗しました", code="file_hash_mismatch")
        return path

    def get_input_file(
        self,
        file_id: str,
        *,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        action_id: str | None = None,
        project_id: UUID | str | None = None,
    ) -> tuple[ActionFileMetadata, Path]:
        file_id = _file_id(file_id)
        metadata = self._read_metadata(self._input_metadata_path(file_id), file_id=file_id)
        self._validate_scope(metadata, app_id=app_id, owner_user_id=owner_user_id, project_id=project_id, action_id=action_id, kind="input")
        path = self._verify_blob(metadata, self._input_blob_path(file_id))
        return metadata, path

    resolve_input_file = get_input_file

    def collect_input_references(self, value: Any) -> list[tuple[dict[str, Any], str]]:
        """Find file reference dictionaries and reject path/URL references.

        Each tuple contains the mutable reference dictionary and a dotted
        location useful to callers when reporting the offending field.
        """
        found: list[tuple[dict[str, Any], str]] = []

        def walk(node: Any, location: str) -> None:
            if isinstance(node, Mapping):
                keys = {str(key).casefold() for key in node.keys()}
                forbidden = keys.intersection(_FORBIDDEN_FILE_REFERENCE_KEYS)
                if forbidden:
                    raise AppActionFileForbidden("Action inputではfile_id以外のpath/URLを指定できません", code="file_reference_path_forbidden")
                if "file_id" in keys:
                    lower_map = {str(key).casefold(): key for key in node.keys()}
                    if any(key not in _FILE_METADATA_KEYS for key in keys):
                        raise AppActionFileValidationError("file referenceに未対応のmetadataがあります", code="invalid_file_reference")
                    raw_id = node.get(lower_map["file_id"])
                    _file_id(raw_id)
                    kind_key = lower_map.get("kind")
                    if kind_key is not None and str(node.get(kind_key) or "").casefold() != "input":
                        raise AppActionFileForbidden("file referenceのkindがinputではありません", code="file_kind_forbidden")
                    found.append((dict(node), location or "$"))
                    return
                for key, child in node.items():
                    walk(child, f"{location}.{key}" if location else str(key))
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    walk(child, f"{location}[{index}]")

        walk(value, "")
        return found

    def validate_input_references(
        self,
        input_json: Mapping[str, Any],
        *,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        action_id: str,
        project_id: UUID | str | None = None,
    ) -> list[ActionFileReference]:
        if not isinstance(input_json, Mapping):
            raise AppActionFileValidationError("inputはobjectで指定してください", code="invalid_input")
        refs: list[ActionFileReference] = []
        for reference, location in self.collect_input_references(input_json):
            file_id = _file_id(reference.get("file_id"))
            metadata, _path = self.get_input_file(file_id, app_id=app_id, owner_user_id=owner_user_id, action_id=action_id, project_id=project_id)
            for key, expected in (("filename", metadata.filename), ("name", metadata.filename), ("content_type", metadata.content_type), ("size_bytes", metadata.size_bytes), ("sha256", metadata.sha256)):
                if key in reference and reference[key] is not None and str(reference[key]).casefold() != str(expected).casefold():
                    raise AppActionFileConflict(f"file reference metadataが一致しません: {location}.{key}", code="file_metadata_mismatch")
            refs.append(ActionFileReference(metadata=metadata, relative_path=f"{metadata.file_id}.bin"))
        return refs

    validate_input_files = validate_input_references

    def normalize_input_references(self, input_json: Mapping[str, Any], refs: Iterable[ActionFileReference]) -> dict[str, Any]:
        """Return a deep JSON copy with only server-verified file metadata."""
        by_id = {item.metadata.file_id: item.metadata for item in refs}

        def normalize(node: Any) -> Any:
            if isinstance(node, Mapping):
                if any(str(key).casefold() == "file_id" for key in node):
                    file_key = next(key for key in node if str(key).casefold() == "file_id")
                    metadata = by_id.get(_file_id(node[file_key]))
                    if metadata is None:
                        raise AppActionFileForbidden("未検証のfile referenceです", code="file_reference_unverified")
                    result = {"file_id": metadata.file_id, "filename": metadata.filename, "content_type": metadata.content_type, "size_bytes": metadata.size_bytes, "sha256": metadata.sha256}
                    return result
                return {str(key): normalize(child) for key, child in node.items()}
            if isinstance(node, list):
                return [normalize(child) for child in node]
            return node

        result = normalize(input_json)
        if not isinstance(result, dict):
            raise AppActionFileValidationError("inputはobjectで指定してください", code="invalid_input")
        return result

    def materialize_input_files(
        self,
        run_id: UUID | str,
        refs: Iterable[ActionFileReference],
        *,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        action_id: str,
        project_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """Copy verified input bytes to the immutable run input namespace.

        The returned manifest contains a fixed root plus opaque references,
        which is the only file protocol the server/PC worker needs.  Caller
        supplied paths never enter the manifest.
        """
        run = _as_uuid(run_id, "run_id") or ""
        app = _as_uuid(app_id, "app_id") or ""
        owner = _as_uuid(owner_user_id, "owner_user_id") or ""
        project = _as_uuid(project_id, "project_id", required=False)
        validated: list[tuple[ActionFileMetadata, Path]] = []
        for reference in refs:
            if not isinstance(reference, ActionFileReference):
                raise AppActionFileValidationError("refsは検証済みActionFileReferenceで指定してください", code="invalid_file_reference")
            validated.append(self.get_input_file(reference.metadata.file_id, app_id=app, owner_user_id=owner, action_id=action_id, project_id=project))
        root = self._ensure_run_subdir(run, "inputs")
        materialized: list[dict[str, Any]] = []
        seen_file_ids: set[str] = set()
        for metadata, source in validated:
            if metadata.file_id in seen_file_ids:
                continue
            seen_file_ids.add(metadata.file_id)
            destination = self._run_blob_path(run, metadata.file_id, kind="input")
            metadata_path = self._run_metadata_path(run, metadata.file_id, kind="input")
            if metadata_path.exists():
                existing = self._read_metadata(metadata_path, file_id=metadata.file_id)
                if existing != metadata:
                    raise AppActionFileConflict("run input metadataが既存run bindingと一致しません", code="run_input_immutable_conflict")
            if destination.exists():
                self._verify_blob(metadata, destination)
            else:
                self._copy_atomic(source, destination)
            self._verify_blob(metadata, destination)
            run_metadata = metadata.to_dict()
            # Keep the original input metadata shape valid.  The binding to a
            # run is recorded separately so a verifier can still parse this
            # sidecar as an input file without confusing it with an output.
            run_metadata.update({"kind": "input", "bound_run_id": run, "relative_path": f"{metadata.file_id}.bin"})
            # The run copy metadata records its immutable run binding; it is
            # separate from the pre-run upload sidecar and never changes it.
            if not metadata_path.exists():
                self._atomic_write(metadata_path, _canonical_json(run_metadata))
            materialized.append({"file_id": metadata.file_id, "filename": metadata.filename, "content_type": metadata.content_type, "size_bytes": metadata.size_bytes, "sha256": metadata.sha256, "relative_path": f"{metadata.file_id}.bin"})
        return {
            "protocol_version": FILE_PROTOCOL_VERSION,
            "root": str(root),
            "files": materialized,
            "env": {
                FILE_PROTOCOL_ENV: FILE_PROTOCOL_VERSION,
                FILE_ROOT_ENV: str(root),
                INPUT_FILES_ENV: json.dumps(materialized, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            },
        }

    prepare_run_inputs = materialize_input_files

    async def store_output_file(
        self,
        upload: Any,
        *,
        run_id: UUID | str,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None = None,
        action_id: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
        ttl_seconds: int | None = None,
        source_ref: str | None = None,
        _lock_acquired: bool = False,
    ) -> ActionFileMetadata:
        """Stream one output file into its exact run-owned namespace."""
        run = _as_uuid(run_id, "run_id") or ""
        app = _as_uuid(app_id, "app_id") or ""
        owner = _as_uuid(owner_user_id, "owner_user_id") or ""
        project = _as_uuid(project_id, "project_id", required=False)
        normalized_source_ref = _safe_relative(source_ref) if source_ref else None
        safe_name = _safe_filename(filename if filename is not None else getattr(upload, "filename", None))
        safe_type = _safe_content_type(content_type if content_type is not None else getattr(upload, "content_type", None), safe_name)
        if not callable(getattr(upload, "read", None)):
            raise AppActionFileValidationError("uploadが読み込めません", code="invalid_upload")
        supplied_hash = str(expected_sha256 or "").strip().lower()
        if supplied_hash and not _SHA256_RE.fullmatch(supplied_hash):
            raise AppActionFileValidationError("expected_sha256が不正です", code="invalid_sha256")
        try:
            expected_size = None if expected_size_bytes is None else int(expected_size_bytes)
        except (TypeError, ValueError) as exc:
            raise AppActionFileValidationError("expected_size_bytesが不正です", code="invalid_file_size") from exc
        if expected_size is not None and (expected_size < 0 or expected_size > self.max_file_bytes):
            raise AppActionFileValidationError("expected_size_bytesが不正です", code="invalid_file_size")
        try:
            ttl = self.output_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise AppActionFileValidationError("output fileの有効期限が不正です", code="invalid_file_expiry") from exc
        if ttl <= 0 or ttl > 30 * 24 * 60 * 60:
            raise AppActionFileValidationError("output fileの有効期限が不正です", code="invalid_file_expiry")
        if normalized_source_ref and not _lock_acquired:
            lock = self._output_capture_lock(run, normalized_source_ref)
            await lock.acquire()
            try:
                return await self.store_output_file(
                    upload,
                    run_id=run,
                    app_id=app,
                    owner_user_id=owner,
                    project_id=project,
                    action_id=action_id,
                    filename=safe_name,
                    content_type=safe_type,
                    expected_sha256=supplied_hash,
                    expected_size_bytes=expected_size,
                    ttl_seconds=ttl,
                    source_ref=normalized_source_ref,
                    _lock_acquired=True,
                )
            finally:
                lock.release()
        if normalized_source_ref and supplied_hash and expected_size is not None:
            existing = self._find_existing_output(
                run,
                source_ref=normalized_source_ref,
                app_id=app,
                owner_user_id=owner,
                project_id=project,
                action_id=action_id,
                sha256=supplied_hash,
                size_bytes=expected_size,
            )
            if existing is not None:
                return existing
        file_id = self._new_file_id()
        destination = self._run_blob_path(run, file_id, kind="output")
        self._ensure_run_subdir(run, "outputs")
        size, digest = await self._write_upload(upload, destination)
        if normalized_source_ref:
            try:
                existing = self._find_existing_output(
                    run,
                    source_ref=normalized_source_ref,
                    app_id=app,
                    owner_user_id=owner,
                    project_id=project,
                    action_id=action_id,
                    sha256=digest,
                    size_bytes=size,
                )
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            if existing is not None:
                destination.unlink(missing_ok=True)
                return existing
        if expected_size is not None and expected_size != size:
            destination.unlink(missing_ok=True)
            raise AppActionFileConflict("outputのsizeが一致しません", code="file_size_mismatch")
        if supplied_hash and supplied_hash != digest:
            destination.unlink(missing_ok=True)
            raise AppActionFileConflict("outputのSHA-256が一致しません", code="file_hash_mismatch")
        metadata = self._metadata_payload(
            file_id=file_id,
            kind="output",
            app_id=app,
            owner_user_id=owner,
            project_id=project,
            action_id=action_id,
            run_id=run,
            filename=safe_name,
            content_type=safe_type,
            size_bytes=size,
            sha256=digest,
            expires_at=_utc_now() + timedelta(seconds=ttl),
            source_ref=normalized_source_ref,
        )
        try:
            ActionFileMetadata.from_dict(metadata)
            self._atomic_write(self._run_metadata_path(run, file_id, kind="output"), _canonical_json(metadata))
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return ActionFileMetadata.from_dict(metadata)

    upload_output_file = store_output_file

    def _output_capture_lock(self, run_id: UUID | str, source_ref: str) -> AppOperationLock:
        """Return a cross-process lock for one run/remote output reference."""
        run = _as_uuid(run_id, "run_id") or ""
        normalized_ref = _safe_relative(source_ref)
        digest = hashlib.sha256(f"{run}\0{normalized_ref}".encode("utf-8")).hexdigest()
        lock_root = self.root / ".locks"
        self._assert_storage_directory(lock_root, "output lock rootが不正です")
        self._ensure_root()
        lock_root.mkdir(exist_ok=True)
        self._assert_storage_directory(lock_root, "output lock rootが不正です")
        lock_path = lock_root / f"output_{digest}.lock"
        if _is_storage_link_or_reparse(lock_path):
            raise AppActionFileUnavailable("output lock fileが不正です", code="storage_root_invalid")
        return AppOperationLock(lock_path)

    def _find_existing_output(
        self,
        run_id: UUID | str,
        *,
        source_ref: str,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None,
        action_id: str | None,
        sha256: str,
        size_bytes: int,
    ) -> ActionFileMetadata | None:
        """Return one matching captured output without creating a duplicate."""
        run = _as_uuid(run_id, "run_id") or ""
        normalized_ref = _safe_relative(source_ref)
        expected_hash = str(sha256 or "").strip().lower()
        if not _SHA256_RE.fullmatch(expected_hash):
            raise AppActionFileValidationError("output SHA-256が不正です", code="invalid_sha256")
        directory = self._run_subdir(run, "outputs")
        for metadata_path in sorted(directory.glob("*.json")):
            if metadata_path.is_symlink() or not metadata_path.is_file():
                continue
            file_id = metadata_path.stem
            try:
                metadata = self._read_metadata(metadata_path, file_id=file_id)
            except AppActionFileError:
                continue
            if metadata.kind != "output" or metadata.source_ref != normalized_ref:
                continue
            self._validate_scope(
                metadata,
                app_id=app_id,
                owner_user_id=owner_user_id,
                project_id=project_id,
                action_id=action_id,
                run_id=run,
                kind="output",
                check_expiry=False,
            )
            if metadata.sha256.casefold() != expected_hash.casefold() or metadata.size_bytes != int(size_bytes):
                raise AppActionFileConflict("同じremote output refのintegrity値が変わりました", code="output_ref_conflict")
            self._verify_blob(metadata, self._run_blob_path(run, metadata.file_id, kind="output"))
            return metadata
        return None

    def store_output_path(
        self,
        source_path: str | os.PathLike[str],
        *,
        allowed_root: str | os.PathLike[str],
        run_id: UUID | str,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None = None,
        action_id: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
        ttl_seconds: int | None = None,
        source_ref: str | None = None,
    ) -> ActionFileMetadata:
        """Ingest a worker output only when it is already inside an allowed root."""
        source = Path(source_path)
        raw_root = Path(allowed_root)
        if raw_root.is_symlink() or source.is_symlink():
            raise AppActionFileForbidden("worker outputのsymlinkは許可されません", code="output_symlink_forbidden")
        try:
            root = raw_root.resolve(strict=True)
            if not root.is_dir():
                raise OSError("allowed_root is not a directory")
            source_resolved = source.resolve(strict=True)
            source_resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise AppActionFileForbidden("worker outputが許可root外です", code="output_root_forbidden") from exc
        if not source_resolved.is_file():
            raise AppActionFileNotFound("worker outputがありません", code="output_not_found")
        if source_resolved.stat().st_size > self.max_file_bytes:
            raise AppActionFileValidationError("output sizeが上限を超えています", code="file_too_large")
        # Use a small synchronous adapter; the copy itself remains bounded and
        # ends in the same async-safe storage path as HTTP uploads.
        class _PathUpload:
            def __init__(self, path: Path, name: str, media_type: str | None):
                self._stream = path.open("rb")
                self.filename = name
                self.content_type = media_type

            def read(self, size: int = -1) -> bytes:
                return self._stream.read(size)

        upload = _PathUpload(source_resolved, filename or source_resolved.name, content_type)
        try:
            metadata = _run_coro_sync(
                self.store_output_file(
                    upload,
                    run_id=run_id,
                    app_id=app_id,
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    action_id=action_id,
                    filename=filename or source_resolved.name,
                    content_type=content_type,
                    expected_sha256=expected_sha256,
                    expected_size_bytes=expected_size_bytes,
                    ttl_seconds=ttl_seconds,
                    source_ref=source_ref,
                )
            )
            return metadata
        finally:
            upload._stream.close()

    def _output_metadata(self, run_id: UUID | str, file_id: str) -> ActionFileMetadata:
        file_id = _file_id(file_id)
        return self._read_metadata(self._run_metadata_path(run_id, file_id, kind="output"), file_id=file_id)

    def get_output_file(
        self,
        file_id: str,
        *,
        run_id: UUID | str,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None = None,
        action_id: str | None = None,
        check_expiry: bool = True,
    ) -> tuple[ActionFileMetadata, Path]:
        metadata = self._output_metadata(run_id, file_id)
        self._validate_scope(metadata, app_id=app_id, owner_user_id=owner_user_id, project_id=project_id, action_id=action_id, run_id=run_id, kind="output", check_expiry=check_expiry)
        path = self._verify_blob(metadata, self._run_blob_path(run_id, file_id, kind="output"))
        return metadata, path

    def get_output_file_metadata(
        self,
        file_id: str,
        *,
        run_id: UUID | str,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None = None,
        action_id: str | None = None,
        check_expiry: bool = True,
    ) -> tuple[ActionFileMetadata, Path]:
        """Resolve scoped output metadata and file size without hashing bytes.

        This is only for download preflight.  The ordinary download path must
        continue to call :meth:`get_output_file` and verify the full digest.
        """
        metadata = self._output_metadata(run_id, file_id)
        self._validate_scope(metadata, app_id=app_id, owner_user_id=owner_user_id, project_id=project_id, action_id=action_id, run_id=run_id, kind="output", check_expiry=check_expiry)
        path = self._verify_blob_metadata(metadata, self._run_blob_path(run_id, file_id, kind="output"))
        return metadata, path

    resolve_output_file = get_output_file

    def list_output_files(
        self,
        run_id: UUID | str,
        *,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None = None,
        action_id: str | None = None,
    ) -> list[ActionFileMetadata]:
        run = _as_uuid(run_id, "run_id") or ""
        directory = self._run_subdir(run, "outputs")
        result: list[ActionFileMetadata] = []
        for metadata_path in sorted(directory.glob("*.json")):
            if metadata_path.is_symlink() or not metadata_path.is_file():
                continue
            file_id = metadata_path.stem
            try:
                metadata = self._read_metadata(metadata_path, file_id=file_id)
                self._validate_scope(metadata, app_id=app_id, owner_user_id=owner_user_id, project_id=project_id, action_id=action_id, run_id=run, kind="output")
                self._verify_blob(metadata, self._run_blob_path(run, file_id, kind="output"))
            except AppActionFileError:
                # A corrupt/unowned sidecar must never make another caller's
                # list reveal data.  Callers addressing that exact id still
                # receive a precise denial/not-found from get_output_file.
                continue
            result.append(metadata)
        return result

    list_run_files = list_output_files

    def get_file_for_run(
        self,
        file_id: str,
        *,
        run_id: UUID | str,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None = None,
        action_id: str | None = None,
    ) -> tuple[ActionFileMetadata, Path]:
        return self.get_output_file(file_id, run_id=run_id, app_id=app_id, owner_user_id=owner_user_id, project_id=project_id, action_id=action_id)

    def worker_file_environment(self, run_id: UUID | str, refs: Iterable[ActionFileReference]) -> dict[str, str]:
        """Return fixed env values for the server/PC action worker."""
        run = _as_uuid(run_id, "run_id") or ""
        root = self._run_subdir(run, "inputs")
        entries: list[dict[str, Any]] = []
        for ref in refs:
            if not isinstance(ref, ActionFileReference) or ref.metadata.kind != "input":
                raise AppActionFileValidationError("refsは検証済みinput ActionFileReferenceで指定してください", code="invalid_file_reference")
            self._verify_blob(ref.metadata, self._run_blob_path(run, ref.metadata.file_id, kind="input"))
            entries.append({"file_id": ref.metadata.file_id, "filename": ref.metadata.filename, "content_type": ref.metadata.content_type, "size_bytes": ref.metadata.size_bytes, "sha256": ref.metadata.sha256, "relative_path": f"{ref.metadata.file_id}.bin"})
        return {
            FILE_PROTOCOL_ENV: FILE_PROTOCOL_VERSION,
            FILE_ROOT_ENV: str(root),
            INPUT_FILES_ENV: json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        }

    def prepare_inputs(
        self,
        input_json: Mapping[str, Any],
        *,
        run_id: UUID | str,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        action_id: str,
        project_id: UUID | str | None = None,
    ) -> dict[str, Any]:
        """Validate and stage one Action input before a worker is started.

        This is the shared server/PC boundary.  The returned ``input`` is a
        detached JSON projection containing only verified opaque file
        metadata.  ``references`` is retained for internal callers, while
        ``refs`` is a serializable metadata projection for transport code.
        The run input bytes are copied and integrity checked before this
        method returns, so dispatchers can safely call it before committing a
        process start.
        """
        if not isinstance(input_json, Mapping):
            raise AppActionFileValidationError("inputはobjectで指定してください", code="invalid_input")
        refs = self.validate_input_references(
            input_json,
            app_id=app_id,
            owner_user_id=owner_user_id,
            action_id=action_id,
            project_id=project_id,
        )
        normalized = self.normalize_input_references(input_json, refs)
        manifest = self.materialize_input_files(
            run_id,
            refs,
            app_id=app_id,
            owner_user_id=owner_user_id,
            action_id=action_id,
            project_id=project_id,
        )
        environment = self.worker_file_environment(run_id, refs)
        return {
            "run_id": _as_uuid(run_id, "run_id"),
            "input": normalized,
            "references": tuple(refs),
            "refs": [ref.metadata.to_dict() for ref in refs],
            "manifest": manifest,
            "environment": environment,
        }

    def _coerce_input_references(self, refs: Any) -> list[ActionFileReference]:
        """Accept prepared references or their full metadata projection."""
        if isinstance(refs, Mapping):
            refs = refs.get("references", refs.get("refs", ()))
        if refs is None or isinstance(refs, (str, bytes, bytearray)):
            raise AppActionFileValidationError("refsが不正です", code="invalid_file_reference")
        result: list[ActionFileReference] = []
        try:
            values = list(refs)
        except TypeError as exc:
            raise AppActionFileValidationError("refsが不正です", code="invalid_file_reference") from exc
        for value in values:
            if isinstance(value, ActionFileReference):
                result.append(value)
                continue
            if isinstance(value, Mapping):
                metadata = ActionFileMetadata.from_dict(value)
                if metadata.kind != "input":
                    raise AppActionFileValidationError("refsはinput fileで指定してください", code="invalid_file_reference")
                result.append(ActionFileReference(metadata=metadata, relative_path=f"{metadata.file_id}.bin"))
                continue
            raise AppActionFileValidationError("refsは検証済みActionFileReferenceで指定してください", code="invalid_file_reference")
        return result

    def transfer_inputs(
        self,
        run_id: UUID | str,
        refs: Iterable[ActionFileReference] | Mapping[str, Any],
        *,
        chunk_size: int = CHUNK_SIZE,
    ) -> Iterable[dict[str, Any]]:
        """Yield verified run input bytes for a server/PC transport.

        ``ref`` is always a run-relative ``files/<opaque-id>.bin`` value and
        each yielded item contains the actual bytes.  A caller must consume
        this generator before spawning the worker; an unmaterialized or
        tampered run copy fails closed through ``_verify_blob``.
        """
        try:
            size = int(chunk_size)
        except (TypeError, ValueError) as exc:
            raise AppActionFileValidationError("chunk_sizeが不正です", code="invalid_chunk_size") from exc
        if size <= 0 or size > CHUNK_SIZE:
            raise AppActionFileValidationError("chunk_sizeが不正です", code="invalid_chunk_size")
        run = _as_uuid(run_id, "run_id") or ""
        for ref in self._coerce_input_references(refs):
            path = self._run_blob_path(run, ref.metadata.file_id, kind="input")
            self._verify_blob(ref.metadata, path)
            offset = 0
            try:
                with path.open("rb") as stream:
                    while True:
                        data = stream.read(size)
                        if not data:
                            break
                        yield {
                            "run_id": run,
                            "file_id": ref.metadata.file_id,
                            "ref": f"files/{ref.metadata.file_id}.bin",
                            "filename": ref.metadata.filename,
                            "content_type": ref.metadata.content_type,
                            "size_bytes": ref.metadata.size_bytes,
                            "sha256": ref.metadata.sha256,
                            "offset": offset,
                            "data": data,
                        }
                        offset += len(data)
            except OSError as exc:
                raise AppActionFileUnavailable("run input bytesを読み込めません", code="file_bytes_unavailable") from exc

    def capture_output_bytes(
        self,
        data: bytes | bytearray | memoryview,
        *,
        run_id: UUID | str,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None = None,
        action_id: str | None = None,
        filename: str,
        content_type: str | None = None,
        expected_sha256: str | None = None,
        expected_size_bytes: int | None = None,
        ttl_seconds: int | None = None,
        source_ref: str | None = None,
    ) -> ActionFileMetadata:
        """Capture remote/server output bytes into the run-owned store."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise AppActionFileValidationError("output bytesが不正です", code="invalid_upload")
        payload = bytes(data)
        if len(payload) > self.max_file_bytes:
            raise AppActionFileValidationError("output sizeが上限を超えています", code="file_too_large")
        upload = BytesIO(payload)
        upload.filename = filename  # type: ignore[attr-defined]
        upload.content_type = content_type  # type: ignore[attr-defined]
        return _run_coro_sync(
            self.store_output_file(
                upload,
                run_id=run_id,
                app_id=app_id,
                owner_user_id=owner_user_id,
                project_id=project_id,
                action_id=action_id,
                filename=filename,
                content_type=content_type,
                expected_sha256=expected_sha256,
                expected_size_bytes=expected_size_bytes,
                ttl_seconds=ttl_seconds,
                source_ref=source_ref,
            )
        )

    def capture_outputs(
        self,
        outputs: Iterable[Mapping[str, Any] | str | os.PathLike[str]],
        *,
        run_id: UUID | str,
        app_id: UUID | str,
        owner_user_id: UUID | str,
        project_id: UUID | str | None = None,
        action_id: str | None = None,
        allowed_root: str | os.PathLike[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> list[ActionFileMetadata]:
        """Capture server paths or remote byte payloads as opaque run files.

        Path entries require an explicit allowed root.  Byte entries are
        copied into the same run-owned namespace as HTTP output uploads;
        metadata-only entries are rejected because they cannot establish
        output bytes or integrity.
        """
        result: list[ActionFileMetadata] = []
        for item in outputs:
            if isinstance(item, Mapping):
                raw_data = item.get("data", item.get("bytes"))
                if raw_data is not None:
                    filename = str(item.get("filename") or item.get("name") or "")
                    if not filename:
                        raise AppActionFileValidationError("output filenameが必要です", code="invalid_filename")
                    result.append(
                        self.capture_output_bytes(
                            raw_data,
                            run_id=run_id,
                            app_id=app_id,
                            owner_user_id=owner_user_id,
                            project_id=project_id,
                            action_id=action_id,
                            filename=filename,
                            content_type=str(item.get("content_type") or "") or None,
                            expected_sha256=item.get("sha256") or item.get("expected_sha256"),
                            expected_size_bytes=item.get("size_bytes", item.get("expected_size_bytes")),
                            ttl_seconds=ttl_seconds,
                            source_ref=item.get("remote_ref") or item.get("ref"),
                        )
                    )
                    continue
                source = item.get("source_path", item.get("path"))
                if source is None:
                    raise AppActionFileValidationError("output metadataだけではcaptureできません", code="output_bytes_missing")
                item_root = item.get("allowed_root", allowed_root)
                if item_root is None:
                    raise AppActionFileForbidden("worker outputのallowed_rootが必要です", code="output_root_required")
                source_path = Path(source)
                if not source_path.is_absolute():
                    source_path = Path(item_root) / source_path
                source_ref = item.get("remote_ref") or item.get("ref")
                if not source_ref:
                    try:
                        source_ref = source_path.resolve().relative_to(Path(item_root).resolve()).as_posix()
                    except ValueError:
                        source_ref = None
                result.append(
                    self.store_output_path(
                        source_path,
                        allowed_root=item_root,
                        run_id=run_id,
                        app_id=app_id,
                        owner_user_id=owner_user_id,
                        project_id=project_id,
                        action_id=action_id,
                        filename=item.get("filename") or item.get("name"),
                        content_type=item.get("content_type"),
                        expected_sha256=item.get("sha256") or item.get("expected_sha256"),
                        expected_size_bytes=item.get("size_bytes", item.get("expected_size_bytes")),
                        ttl_seconds=ttl_seconds,
                        source_ref=source_ref,
                    )
                )
                continue
            if allowed_root is None:
                raise AppActionFileForbidden("worker outputのallowed_rootが必要です", code="output_root_required")
            source_path = Path(item)
            if not source_path.is_absolute():
                source_path = Path(allowed_root) / source_path
            try:
                source_ref = source_path.resolve().relative_to(Path(allowed_root).resolve()).as_posix()
            except ValueError:
                source_ref = None
            result.append(
                self.store_output_path(
                    source_path,
                    allowed_root=allowed_root,
                    run_id=run_id,
                    app_id=app_id,
                    owner_user_id=owner_user_id,
                    project_id=project_id,
                    action_id=action_id,
                    ttl_seconds=ttl_seconds,
                    source_ref=source_ref,
                )
            )
        return result


def _run_coro_sync(coro: Any) -> Any:
    """Run an async helper from the synchronous worker ingest adapter."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # ``store_output_path`` is called by the execution worker, which is
    # normally synchronous.  If an async loop is already active, use a helper
    # thread rather than nesting asyncio.run and corrupting the caller loop.
    import threading

    holder: dict[str, Any] = {}
    error: list[BaseException] = []

    def runner() -> None:
        try:
            holder["result"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive boundary
            error.append(exc)

    thread = threading.Thread(target=runner, name="aoitalk-action-file-ingest", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return holder.get("result")


__all__ = [
    "ActionFileMetadata",
    "ActionFileReference",
    "AppActionFileConflict",
    "AppActionFileError",
    "AppActionFileForbidden",
    "AppActionFileNotFound",
    "AppActionFileService",
    "AppActionFileUnavailable",
    "AppActionFileValidationError",
    "DEFAULT_INPUT_TTL_SECONDS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_OUTPUT_TTL_SECONDS",
    "FILE_PROTOCOL_ENV",
    "FILE_PROTOCOL_VERSION",
    "FILE_ROOT_ENV",
    "INPUT_FILES_ENV",
]
