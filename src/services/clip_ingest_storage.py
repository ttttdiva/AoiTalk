"""Safe, user-scoped storage for Docs ClipIngest uploads.

The ClipIngest request does not know its final ``KnowledgeNode`` until after
the planner has run.  Uploads therefore live in a short-lived per-user
staging directory first and are promoted to the existing Docs attachment
namespace only after the node target is known.  Metadata is kept next to the
payload as a small JSON sidecar so this layer does not need a new database
table (``KnowledgeAttachment`` remains the durable source of truth).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import mimetypes
import os
import re
import shutil
import stat
import threading
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Iterable
from uuid import UUID, uuid4

class ClipUploadError(RuntimeError):
    """A staging or promotion request is invalid or cannot be completed."""


_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_MAX_FILENAME = 255
_CHUNK_SIZE = 1024 * 1024
_DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
_DEFAULT_MAX_FILES = 32
_DEFAULT_STAGING_TTL_SECONDS = 24 * 60 * 60
_DEFAULT_GC_MAX_ENTRIES = 32
_DEFAULT_GC_BUDGET_SECONDS = 0.05
_DEFAULT_GLOBAL_GC_INTERVAL_SECONDS = 5 * 60
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_IMAGE_SUFFIXES = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg",
    ".png", ".svg", ".tif", ".tiff", ".webp",
}


def _parse_uuid(value: Any, label: str = "upload_id") -> UUID:
    try:
        parsed = UUID(str(value).strip())
    except (TypeError, ValueError, AttributeError) as exc:
        raise ClipUploadError(f"{label}が不正です") from exc
    return parsed


def _safe_name(value: Any) -> str:
    """Return a display name, never a path.

    ``Path.name`` handles both POSIX and Windows separators poorly when the
    server is running on the other OS, so both separators are normalized
    before extracting the final component.  The result is only used for
    display/extension selection; the actual payload file uses a generated
    UUID name.
    """

    raw = unicodedata.normalize("NFC", str(value or "")).replace("\\", "/")
    raw = raw.rsplit("/", 1)[-1]
    raw = _CONTROL_CHARS_RE.sub("", raw).strip().strip(".")
    if raw in {"", ".", ".."}:
        raw = "upload"
    # Avoid platform-reserved names and make the metadata bounded.
    return raw[:_MAX_FILENAME] or "upload"


def _assert_under(path: Path, root: Path, *, allow_missing: bool = True) -> Path:
    """Resolve ``path`` and ensure it remains below ``root``."""

    if _is_storage_link_or_reparse(root):
        raise ClipUploadError("symlink経由のstaging rootは許可されません")
    root_resolved = root.resolve()
    try:
        lexical = path.absolute()
        lexical.relative_to(root_resolved)
        _reject_link_components(root_resolved, lexical.relative_to(root_resolved))
        resolved = path.resolve(strict=not allow_missing)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise ClipUploadError("staging pathがworkspace rootの外へ出ます") from exc
    return resolved


def _is_storage_link_or_reparse(path: Path) -> bool:
    """Return true for POSIX symlinks and Windows junction/reparse entries."""

    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        item_stat = path.lstat()
        return bool(
            getattr(item_stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError):
        # Fail closed when a path cannot be inspected safely.
        return True


def _reject_link_components(root: Path, relative: Path) -> None:
    """Reject symlink/reparse aliases in every existing path component."""

    current = root
    for component in relative.parts:
        current = current / component
        if _is_storage_link_or_reparse(current):
            raise ClipUploadError("シンボリックリンク経由のstagingアクセスは許可されません")


def _trusted_workspace_root(value: str | os.PathLike[str] | None) -> Path:
    """Create/resolve a workspace root only when no component is a link.

    ``Path.resolve`` follows a configured root symlink, which would silently
    move ClipIngest's namespace outside the operator's intended workspace.
    Validate the lexical path first, create missing directories, then retain a
    canonical resolved anchor for all later under-root checks.
    """

    raw = value or os.environ.get("AOITALK_WORKSPACES_DIR", "./workspaces")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = Path(os.path.abspath(os.fspath(candidate)))
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current = current / component
        if _is_storage_link_or_reparse(current):
            raise ClipUploadError("workspace rootのシンボリックリンクは許可されません")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ClipUploadError("workspace rootを作成できません") from exc
    # Recheck after mkdir to close a simple replacement race.
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current = current / component
        if _is_storage_link_or_reparse(current):
            raise ClipUploadError("workspace rootのシンボリックリンクは許可されません")
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise ClipUploadError("workspace rootを解決できません") from exc


@dataclass(frozen=True)
class ClipUpload:
    """A staged upload and its non-secret metadata."""

    upload_id: str
    user_id: str
    file_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    is_image: bool
    created_at: float
    _payload_path: Path

    @property
    def payload_path(self) -> Path:
        """Internal path accessor used by recognition/promotion only."""

        return self._payload_path

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize metadata without leaking a local absolute path."""

        return {
            "upload_id": self.upload_id,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "is_image": self.is_image,
            "created_at": self.created_at,
        }

    def to_evidence_dict(
        self,
        *,
        recognition_status: str = "not_image",
        recognition_provider: str = "",
        recognition_model: str = "",
        recognition: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        """Build the planner-facing attachment evidence object."""

        value = self.to_public_dict()
        value.update(
            {
                "recognition_status": str(recognition_status or "not_image"),
                "recognition_provider": str(recognition_provider or ""),
                "recognition_model": str(recognition_model or ""),
            }
        )
        if recognition:
            value["recognition"] = str(recognition)[:20_000]
        if error:
            value["error"] = str(error)[:1000]
        return value


class ClipIngestStorage:
    """Filesystem staging/promotion for authenticated ClipIngest users."""

    # Router/service instances are intentionally cheap and are often created
    # more than once in tests or by a worker.  Keep one non-blocking lock per
    # workspace root so two requests cannot scan/remove the same staging tree
    # concurrently.  The lock is process-local; workers in another process
    # still remain safe because every candidate is checked before removal and
    # ``rmtree`` is idempotent under races.
    _gc_locks: ClassVar[dict[str, threading.Lock]] = {}
    _gc_locks_guard: ClassVar[threading.Lock] = threading.Lock()
    _last_global_gc: ClassVar[dict[str, float]] = {}

    def __init__(
        self,
        workspace_root: str | os.PathLike[str] | None = None,
        *,
        max_upload_bytes: int | None = None,
        max_files: int = _DEFAULT_MAX_FILES,
        defer_staging_cleanup: bool = False,
    ) -> None:
        self.workspace_root = _trusted_workspace_root(workspace_root)
        configured = max_upload_bytes
        if configured is None:
            try:
                configured = int(
                    os.environ.get(
                        "AOITALK_DOCS_CLIP_MAX_UPLOAD_BYTES",
                        _DEFAULT_MAX_UPLOAD_BYTES,
                    )
                )
            except (TypeError, ValueError):
                configured = _DEFAULT_MAX_UPLOAD_BYTES
        self.max_upload_bytes = max(1, int(configured))
        self.max_files = max(1, int(max_files))
        # Durable workers copy into the attachment namespace before the DB
        # transaction commits.  Keep the source sidecar/payload until the
        # worker has observed a successful commit so a crash can recover it.
        # Legacy synchronous callers retain the historical move-and-cleanup
        # behavior (the default).
        self.defer_staging_cleanup = bool(defer_staging_cleanup)

    @staticmethod
    def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
        try:
            value = float(os.environ.get(name, default))
        except (TypeError, ValueError):
            value = default
        if value < minimum:
            return minimum
        return value

    def staging_ttl_seconds(self) -> float:
        """Configured lifetime for abandoned staged uploads.

        This is deliberately read at call time so deployments/tests can tune
        cleanup without rebuilding the router.
        """

        return self._env_float(
            "AOITALK_DOCS_CLIP_STAGING_TTL_SECONDS",
            _DEFAULT_STAGING_TTL_SECONDS,
            minimum=0.0,
        )

    def _gc_lock(self) -> threading.Lock:
        key = str(self.workspace_root)
        with self._gc_locks_guard:
            lock = self._gc_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._gc_locks[key] = lock
            return lock

    def _validate_gc_path(self, path: Path, *, require_existing: bool) -> Path:
        """Re-lstat every ancestor before a GC traversal or deletion.

        The constructor validates the workspace root once, but an attacker
        can replace ``_users`` (or a parent component) with a symlink after
        startup.  GC must fail closed instead of following that replacement.
        This check intentionally uses lexical components and ``lstat``-based
        link detection rather than ``Path.resolve``.
        """

        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        current = Path(candidate.anchor)
        for component in candidate.parts[1:]:
            current = current / component
            if _is_storage_link_or_reparse(current):
                raise ClipUploadError("GC対象pathのsymlink/reparseは許可されません")
            if require_existing and not current.exists():
                raise ClipUploadError("GC対象pathが見つかりません")
        return candidate

    def _user_root(self, user_id: Any) -> Path:
        parsed = _parse_uuid(user_id, "user_id")
        root = self.workspace_root / "_users" / f"user_{parsed}"
        # Do not resolve a path supplied by a caller before this check.  The
        # namespace and its direct child are generated from a UUID only.
        return _assert_under(root, self.workspace_root / "_users")

    def staging_root(self, user_id: Any) -> Path:
        return self._user_root(user_id) / "clip-ingest"

    def _upload_dir(self, user_id: Any, upload_id: Any) -> Path:
        parsed = _parse_uuid(upload_id)
        root = self.staging_root(user_id)
        return _assert_under(root / str(parsed), root)

    def _metadata_path(self, user_id: Any, upload_id: Any) -> Path:
        return self._upload_dir(user_id, upload_id) / "metadata.json"

    def _payload_path(self, user_id: Any, upload_id: Any) -> Path:
        return self._upload_dir(user_id, upload_id) / "payload"

    async def stage_upload(self, user_id: Any, upload: Any) -> ClipUpload:
        """Stream one FastAPI ``UploadFile`` into user-scoped staging."""

        if upload is None:
            raise ClipUploadError("アップロードファイルがありません")
        name = _safe_name(getattr(upload, "filename", "upload"))
        mime_type = str(getattr(upload, "content_type", "") or "").split(";", 1)[0].strip().lower()
        mime_type = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        upload_id = uuid4()
        directory = self._upload_dir(user_id, upload_id)
        root = self.staging_root(user_id)
        root.mkdir(parents=True, exist_ok=True)
        directory.mkdir(parents=False, exist_ok=False)
        payload_path = directory / "payload"
        digest = hashlib.sha256()
        total = 0
        try:
            with payload_path.open("xb") as destination:
                while True:
                    reader = getattr(upload, "read", None)
                    if not callable(reader):
                        raise ClipUploadError("アップロードの読み取りに失敗しました")
                    chunk = reader(_CHUNK_SIZE)
                    if inspect.isawaitable(chunk):
                        chunk = await chunk
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise ClipUploadError("アップロードデータが不正です")
                    payload = bytes(chunk)
                    total += len(payload)
                    if total > self.max_upload_bytes:
                        raise ClipUploadError(
                            f"ファイルサイズが上限を超えています（{self.max_upload_bytes} bytes）"
                        )
                    digest.update(payload)
                    destination.write(payload)
            created_at = time.time()
            is_image = mime_type.startswith("image/") or Path(name).suffix.lower() in _IMAGE_SUFFIXES
            metadata = {
                "upload_id": str(upload_id),
                "user_id": str(_parse_uuid(user_id, "user_id")),
                "file_name": name,
                "mime_type": mime_type,
                "size_bytes": total,
                "sha256": digest.hexdigest(),
                "is_image": is_image,
                "created_at": created_at,
            }
            (directory / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
            )
            return ClipUpload(
                upload_id=str(upload_id),
                user_id=metadata["user_id"],
                file_name=name,
                mime_type=mime_type,
                size_bytes=total,
                sha256=metadata["sha256"],
                is_image=bool(metadata["is_image"]),
                created_at=created_at,
                _payload_path=payload_path,
            )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    async def stage_uploads(self, user_id: Any, uploads: Iterable[Any]) -> list[ClipUpload]:
        values = list(uploads)
        if not values:
            return []
        if len(values) > self.max_files:
            raise ClipUploadError(f"一度にアップロードできるファイル数は{self.max_files}件までです")
        staged: list[ClipUpload] = []
        try:
            for upload in values:
                staged.append(await self.stage_upload(user_id, upload))
            return staged
        except Exception:
            await self.cleanup_uploads(user_id, [item.upload_id for item in staged])
            raise

    def resolve_upload(self, user_id: Any, upload_id: Any) -> ClipUpload:
        """Resolve one upload only inside the authenticated user's namespace."""

        metadata_path = self._metadata_path(user_id, upload_id)
        directory = metadata_path.parent
        try:
            payload_path = self._payload_path(user_id, upload_id)
            if metadata_path.is_symlink() or payload_path.is_symlink() or directory.is_symlink():
                raise ClipUploadError("staging fileが不正です")
            if not metadata_path.is_file() or not payload_path.is_file():
                raise ClipUploadError("staging fileが見つかりません")
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        except ClipUploadError:
            raise
        except Exception as exc:
            raise ClipUploadError("staging metadataを読み取れません") from exc
        if not isinstance(raw, dict):
            raise ClipUploadError("staging metadataが不正です")
        expected_user = str(_parse_uuid(user_id, "user_id"))
        if str(raw.get("user_id") or "") != expected_user:
            raise ClipUploadError("別ユーザーのstaging fileは利用できません")
        parsed_id = str(_parse_uuid(upload_id))
        if str(raw.get("upload_id") or "") != parsed_id:
            raise ClipUploadError("staging IDが不正です")
        try:
            size = int(raw.get("size_bytes"))
        except (TypeError, ValueError) as exc:
            raise ClipUploadError("staging size metadataが不正です") from exc
        actual_size = payload_path.stat().st_size
        if size < 0 or actual_size != size or size > self.max_upload_bytes:
            raise ClipUploadError("staging fileのサイズ検証に失敗しました")
        digest = hashlib.sha256()
        with payload_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        if str(raw.get("sha256") or "") != sha256:
            raise ClipUploadError("staging fileのsha256検証に失敗しました")
        name = _safe_name(raw.get("file_name"))
        mime_type = str(raw.get("mime_type") or "application/octet-stream")[:120]
        return ClipUpload(
            upload_id=parsed_id,
            user_id=expected_user,
            file_name=name,
            mime_type=mime_type,
            size_bytes=size,
            sha256=sha256,
            is_image=bool(raw.get("is_image")) or mime_type.startswith("image/"),
            created_at=float(raw.get("created_at") or 0),
            _payload_path=payload_path,
        )

    def resolve_uploads(self, user_id: Any, upload_ids: Iterable[Any]) -> list[ClipUpload]:
        values = [str(value).strip() for value in upload_ids if str(value).strip()]
        if len(values) > self.max_files:
            raise ClipUploadError(f"一度に取り込めるファイル数は{self.max_files}件までです")
        seen: set[str] = set()
        uploads: list[ClipUpload] = []
        for value in values:
            parsed = str(_parse_uuid(value))
            if parsed in seen:
                continue
            seen.add(parsed)
            uploads.append(self.resolve_upload(user_id, parsed))
        return uploads

    def recover_promoted_uploads(
        self,
        user_id: Any,
        metadata: Iterable[Mapping[str, Any]] | None,
    ) -> list[ClipUpload]:
        """Recover uploads whose staging payload was already promoted.

        Promotion deliberately happens before the surrounding Docs transaction
        commits.  A process crash in that small window can therefore leave the
        deterministic attachment file in ``_docs/attachments`` while removing
        (or never persisting) its staging sidecar.  The durable job snapshot
        contains only the allowlisted upload metadata needed to identify that
        file; this method uses it to verify a matching payload without scanning
        any user-controlled path.
        """

        expected_user = str(_parse_uuid(user_id, "user_id"))
        values = list(metadata or [])
        if len(values) > self.max_files:
            raise ClipUploadError(f"一度に取り込めるファイル数は{self.max_files}件までです")
        if not values:
            raise ClipUploadError("promoted attachment metadataがありません")

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in values:
            if not isinstance(raw, Mapping):
                raise ClipUploadError("promoted attachment metadataが不正です")
            parsed_id = str(_parse_uuid(raw.get("upload_id")))
            if parsed_id in seen:
                continue
            seen.add(parsed_id)
            file_name_raw = raw.get("file_name")
            if file_name_raw in (None, ""):
                raise ClipUploadError("promoted attachment file nameが不正です")
            file_name = _safe_name(file_name_raw)
            mime_type = str(raw.get("mime_type") or "application/octet-stream")[:120]
            if not re.fullmatch(
                r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*",
                mime_type,
                re.IGNORECASE,
            ):
                raise ClipUploadError("promoted attachment mime metadataが不正です")
            size_raw = raw.get("size_bytes")
            if isinstance(size_raw, bool):
                raise ClipUploadError("promoted attachment size metadataが不正です")
            try:
                size_bytes = int(size_raw)
            except (TypeError, ValueError) as exc:
                raise ClipUploadError("promoted attachment size metadataが不正です") from exc
            sha256 = str(raw.get("sha256") or "").strip().lower()
            if size_bytes < 0 or size_bytes > self.max_upload_bytes:
                raise ClipUploadError("promoted attachment size metadataが不正です")
            if not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ClipUploadError("promoted attachment sha256 metadataが不正です")
            is_image = raw.get("is_image")
            if not isinstance(is_image, bool):
                is_image = mime_type.startswith("image/") or Path(file_name).suffix.lower() in _IMAGE_SUFFIXES
            created_at_raw = raw.get("created_at", 0)
            if isinstance(created_at_raw, bool):
                created_at = 0.0
            else:
                try:
                    created_at = float(created_at_raw or 0)
                except (TypeError, ValueError):
                    created_at = 0.0
            normalized.append(
                {
                    "upload_id": parsed_id,
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "is_image": bool(is_image),
                    "created_at": created_at,
                }
            )

        attachments_root = self.workspace_root / "_docs" / "attachments"
        try:
            self._validate_gc_path(attachments_root, require_existing=False)
            if not attachments_root.is_dir() or _is_storage_link_or_reparse(attachments_root):
                raise ClipUploadError("promoted attachment namespaceが見つかりません")
            self._validate_gc_path(attachments_root, require_existing=True)
            node_dirs = []
            for node_dir in attachments_root.iterdir():
                if not node_dir.is_dir() or _is_storage_link_or_reparse(node_dir):
                    continue
                try:
                    _parse_uuid(node_dir.name, "node_id")
                    self._validate_gc_path(node_dir, require_existing=True)
                except ClipUploadError:
                    continue
                node_dirs.append(node_dir)
        except ClipUploadError:
            raise
        except OSError as exc:
            raise ClipUploadError("promoted attachment namespaceを読み取れません") from exc

        def _candidate_name_matches(candidate: Path, upload_id: str, file_name: str) -> bool:
            canonical = f"{upload_id}-{file_name}"
            if candidate.name == canonical:
                return True
            prefix = f"{upload_id}-"
            suffix = f"-{file_name}"
            if not candidate.name.startswith(prefix) or not candidate.name.endswith(suffix):
                return False
            middle = candidate.name[len(prefix) : -len(suffix)] if suffix else ""
            try:
                return str(_parse_uuid(middle)) == middle.lower()
            except ClipUploadError:
                return False

        def _matches_expected(candidate: Path, item: dict[str, Any]) -> bool:
            try:
                if (
                    candidate.is_symlink()
                    or _is_storage_link_or_reparse(candidate)
                    or not candidate.is_file()
                    or candidate.stat().st_size != item["size_bytes"]
                ):
                    return False
                digest = hashlib.sha256()
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                        digest.update(chunk)
                return digest.hexdigest() == item["sha256"]
            except OSError:
                return False

        recovered: list[ClipUpload] = []
        for item in normalized:
            found: Path | None = None
            canonical_name = f"{item['upload_id']}-{item['file_name']}"
            for node_dir in sorted(node_dirs, key=lambda value: str(value)):
                try:
                    self._validate_gc_path(attachments_root, require_existing=True)
                    self._validate_gc_path(node_dir, require_existing=True)
                    canonical = node_dir / canonical_name
                    candidates = [canonical]
                    candidates.extend(
                        sorted(
                            (candidate for candidate in node_dir.glob(f"{item['upload_id']}-*") if candidate != canonical),
                            key=lambda value: str(value),
                        )
                    )
                except (ClipUploadError, OSError):
                    continue
                for candidate in candidates:
                    try:
                        self._validate_gc_path(candidate, require_existing=True)
                    except ClipUploadError:
                        continue
                    if candidate.parent != node_dir:
                        continue
                    if not _candidate_name_matches(candidate, item["upload_id"], item["file_name"]):
                        continue
                    if _matches_expected(candidate, item):
                        found = candidate
                        break
                if found is not None:
                    break
            if found is None:
                raise ClipUploadError("promoted attachmentが見つかりません")
            recovered.append(
                ClipUpload(
                    upload_id=item["upload_id"],
                    user_id=expected_user,
                    file_name=item["file_name"],
                    mime_type=item["mime_type"],
                    size_bytes=item["size_bytes"],
                    sha256=item["sha256"],
                    is_image=item["is_image"],
                    created_at=item["created_at"],
                    _payload_path=found,
                )
            )
        return recovered

    def read_for_recognition(self, upload: ClipUpload, *, max_bytes: int | None = None) -> bytes:
        limit = self.max_upload_bytes if max_bytes is None else max(1, int(max_bytes))
        if upload.size_bytes > limit:
            raise ClipUploadError("画像認識へ渡せるファイルサイズの上限を超えています")
        try:
            payload = upload.payload_path.read_bytes()
        except OSError as exc:
            raise ClipUploadError("staging fileを読み取れません") from exc
        if len(payload) != upload.size_bytes:
            raise ClipUploadError("staging fileのサイズが変化しました")
        return payload

    def promote(self, user_id: Any, upload: ClipUpload, node_id: Any) -> Path:
        """Promote a staged payload into the existing Docs attachment namespace.

        Durable workers use a copy/verify/atomic-replace sequence and retain
        staging until their surrounding job transaction commits.  The default
        synchronous path remains an atomic ``os.replace`` move for backwards
        compatibility.
        """

        # ``resolve_upload`` is deliberately repeated at the promotion
        # boundary, closing the race between planner execution and final move.
        target_node = _parse_uuid(node_id, "node_id")
        destination_root = self.workspace_root / "_docs" / "attachments" / str(target_node)
        destination_root = _assert_under(destination_root, self.workspace_root / "_docs" / "attachments")
        destination_root.mkdir(parents=True, exist_ok=True)
        # The upload UUID is the idempotency key.  A worker retry after a
        # process crash may have already moved the payload while the database
        # transaction was rolling back; in that case reuse the verified file
        # rather than creating a random duplicate attachment.
        destination = destination_root / f"{upload.upload_id}-{_safe_name(upload.file_name)}"
        destination = _assert_under(destination, destination_root)

        def _matches_expected(path: Path) -> bool:
            try:
                if not path.is_file() or path.stat().st_size != int(upload.size_bytes):
                    return False
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                        digest.update(chunk)
                return digest.hexdigest() == str(upload.sha256)
            except OSError:
                return False

        try:
            current = self.resolve_upload(user_id, upload.upload_id)
        except ClipUploadError:
            if _matches_expected(destination):
                return destination
            # A prior attempt may have taken the collision fallback because a
            # pre-existing unrelated file occupied the canonical name.  The
            # upload UUID prefix is still an unambiguous, bounded recovery
            # scope; reuse a matching fallback before declaring staging lost.
            try:
                for candidate in destination_root.glob(f"{upload.upload_id}-*"):
                    if candidate != destination and _matches_expected(candidate):
                        return candidate
            except OSError:
                pass
            raise
        if destination.exists() or destination.is_symlink():
            if _matches_expected(destination):
                return destination
            # Preserve an unrelated same-name file rather than overwriting it;
            # the UUID-based canonical path remains deterministic for retries.
            destination = destination_root / f"{current.upload_id}-{uuid4()}-{_safe_name(current.file_name)}"
        if self.defer_staging_cleanup:
            # Never expose a partially copied destination.  The temporary file
            # is generated inside the trusted destination directory, verified
            # against the immutable staging metadata, then atomically replaced.
            temporary = destination_root / (
                f".{current.upload_id}-{uuid4().hex}.tmp"
            )
            temporary = _assert_under(temporary, destination_root)
            copied_size = 0
            copied_digest = hashlib.sha256()
            try:
                with current.payload_path.open("rb") as source, temporary.open("xb") as target:
                    for chunk in iter(lambda: source.read(_CHUNK_SIZE), b""):
                        target.write(chunk)
                        copied_size += len(chunk)
                        copied_digest.update(chunk)
                if (
                    copied_size != int(current.size_bytes)
                    or copied_digest.hexdigest() != str(current.sha256)
                ):
                    raise ClipUploadError("添付ファイルのcopy検証に失敗しました")
                os.replace(temporary, destination)
            except ClipUploadError:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            except OSError as exc:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ClipUploadError("添付ファイルのstaging昇格に失敗しました") from exc
        else:
            try:
                os.replace(current.payload_path, destination)
            except OSError as exc:
                raise ClipUploadError("添付ファイルのstaging昇格に失敗しました") from exc
        return destination

    def to_workspace_relative(self, path: str | os.PathLike[str]) -> str:
        """Return an opaque, workspace-relative attachment path.

        Database/API boundaries must not expose the host's absolute path.  The
        returned POSIX form is stable across Windows and POSIX workers while
        retaining the existing ``_docs/attachments/...`` namespace.
        """

        target = _assert_under(Path(path), self.workspace_root, allow_missing=True)
        try:
            relative = target.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ClipUploadError("attachment pathがworkspace rootの外へ出ます") from exc
        if not relative.parts:
            raise ClipUploadError("attachment pathが不正です")
        return relative.as_posix()

    def resolve_attachment_path(
        self,
        file_path: str | os.PathLike[str],
        *,
        require_file: bool = True,
    ) -> Path:
        """Resolve a stored relative path under the trusted workspace root.

        Relative paths are the canonical representation.  Absolute paths are
        accepted only for legacy rows and only when they remain inside this
        trusted root.  Traversal, symlink/reparse aliases, and missing files
        are rejected before a caller opens or deletes the result.
        """

        raw = str(file_path or "").strip()
        if not raw:
            raise ClipUploadError("attachment pathが空です")
        candidate = Path(raw)
        if candidate.is_absolute():
            target = candidate
        else:
            # Store/API values are canonical POSIX relatives; reject Windows
            # drive prefixes and both separator variants before joining.
            normalized = raw.replace("\\", "/")
            if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
                raise ClipUploadError("attachment pathが不正です")
            parts = tuple(part for part in normalized.split("/") if part not in {""})
            if any(part in {".", ".."} for part in parts):
                raise ClipUploadError("attachment path traversalは許可されません")
            target = self.workspace_root.joinpath(*parts)
        target = _assert_under(target, self.workspace_root, allow_missing=not require_file)
        if require_file:
            if target.is_symlink() or not target.is_file():
                raise ClipUploadError("attachment fileが見つかりません")
        return target

    async def cleanup_uploads(self, user_id: Any, upload_ids: Iterable[Any]) -> int:
        removed = 0
        for value in upload_ids:
            try:
                directory = self._upload_dir(user_id, value)
            except ClipUploadError:
                continue
            if directory.exists() or directory.is_symlink():
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        return removed

    def cleanup_promoted(self, paths: Iterable[str | os.PathLike[str]]) -> int:
        # Keep this as a lexical trusted anchor.  Resolving it here would
        # follow a post-promotion replacement of ``_docs`` or ``attachments``
        # and could redirect rollback cleanup into an external tree.
        root = self.workspace_root / "_docs" / "attachments"
        root_lexical = root.absolute()
        removed = 0
        for raw in paths:
            try:
                target = Path(raw)
                if not target.is_absolute():
                    target = self.workspace_root / target
                target = target.absolute()
                target.relative_to(root_lexical)
                # Validate all ancestors immediately before inspecting or
                # unlinking the candidate; never resolve a potentially
                # replaced attachment root as the trust boundary.
                self._validate_gc_path(root, require_existing=True)
                self._validate_gc_path(target, require_existing=True)
            except (ClipUploadError, OSError, ValueError):
                continue
            if target == root_lexical or target.is_dir() or target.is_symlink():
                continue
            try:
                # Final nofollow-style revalidation closes the ordinary
                # replacement window between stat and unlink as far as the
                # platform's path API permits.
                self._validate_gc_path(root, require_existing=True)
                self._validate_gc_path(target, require_existing=True)
                if target.is_dir() or target.is_symlink() or _is_storage_link_or_reparse(target):
                    continue
                target.unlink(missing_ok=True)
                removed += 1
            except (ClipUploadError, OSError):
                pass
        return removed

    def gc_staging(
        self,
        *,
        max_age_seconds: float | None = None,
        user_id: Any | None = None,
        max_entries: int | None = None,
        time_budget_seconds: float | None = None,
        protected_upload_ids: Iterable[Any] | None = None,
    ) -> int:
        """Remove stale staging directories without blocking requests.

        ``user_id`` limits the scan to one authenticated namespace.  A
        process-local, non-blocking lock prevents concurrent requests from
        traversing/removing the same tree.  ``max_entries`` and
        ``time_budget_seconds`` bound opportunistic scans; a maintenance job
        may pass larger values (or ``None``) when it wants a full sweep.
        Symlinks are ignored and never followed.
        """

        ttl = self.staging_ttl_seconds() if max_age_seconds is None else max(
            0.0, float(max_age_seconds)
        )
        # Durable queued/running jobs keep their upload IDs in the job
        # snapshot while their staging files may legitimately be older than
        # the normal TTL.  Normalize only UUID-shaped values so this optional
        # protection remains fail-soft for legacy maintenance callers.
        protected: set[str] = set()
        for value in protected_upload_ids or ():
            try:
                protected.add(str(_parse_uuid(value)))
            except ClipUploadError:
                continue
        # A direct ``gc_staging()`` call remains a full maintenance sweep for
        # backwards compatibility.  Request paths use ``opportunistic_gc``
        # and always supply explicit bounds.
        entry_limit = None if max_entries is None else max(1, int(max_entries))
        budget = (
            None
            if time_budget_seconds is None
            else max(0.001, float(time_budget_seconds))
        )
        root = self.workspace_root / "_users"
        # Revalidate the complete anchor on every sweep; the constructor's
        # one-time trusted-root check is insufficient against post-startup
        # symlink replacement.
        try:
            self._validate_gc_path(root, require_existing=False)
        except ClipUploadError:
            return 0
        if user_id is None:
            scan_roots: list[Path] | None = None
        else:
            # UUID parsing also prevents a caller from selecting another
            # filesystem path through this maintenance helper.
            try:
                scan_roots = [self.staging_root(user_id)]
            except ClipUploadError:
                # GC is maintenance, not an authorization boundary.  If a
                # malformed/symlinked user namespace is observed, skip it
                # rather than making an otherwise valid request fail.
                return 0
        if not root.exists() and scan_roots is None:
            return 0

        lock = self._gc_lock()
        # Never make an ingest request wait behind another worker's scan.
        if not lock.acquire(blocking=False):
            return 0
        deadline = None if budget is None else time.monotonic() + budget
        cutoff = time.time() - ttl
        examined = 0
        removed = 0
        try:
            if scan_roots is None:
                try:
                    self._validate_gc_path(root, require_existing=True)
                    if not root.is_dir() or _is_storage_link_or_reparse(root):
                        return 0
                    user_roots = list(root.glob("user_*"))
                except (OSError, ClipUploadError):
                    return 0
                scan_roots = [
                    item / "clip-ingest"
                    for item in user_roots
                    if item.is_dir() and not _is_storage_link_or_reparse(item)
                ]
            # Oldest first gives abandoned uploads priority when a bounded
            # sweep has to stop part-way through a large workspace.
            for staging in sorted(scan_roots, key=lambda item: str(item)):
                if entry_limit is not None and examined >= entry_limit:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    self._validate_gc_path(root, require_existing=True)
                    self._validate_gc_path(staging, require_existing=True)
                    if not staging.is_dir() or staging.is_symlink():
                        continue
                    directories = sorted(
                        (
                            item
                            for item in staging.iterdir()
                            if item.is_dir() and not item.is_symlink()
                        ),
                        key=lambda item: item.stat().st_mtime,
                    )
                except OSError:
                    continue
                for directory in directories:
                    if entry_limit is not None and examined >= entry_limit:
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    try:
                        candidate_id = directory.name
                        try:
                            candidate_id = str(_parse_uuid(directory.name))
                        except ClipUploadError:
                            pass
                        if directory.is_symlink() or candidate_id in protected:
                            continue
                        examined += 1
                        if directory.stat().st_mtime >= cutoff:
                            continue
                        # Re-check immediately before deletion so a concurrent
                        # upload that refreshed the directory is retained.
                        if directory.stat().st_mtime < cutoff:
                            # Re-lstat the full ``_users/...`` ancestry as the
                            # final operation before rmtree; a replaced root
                            # must never redirect deletion outside workspace.
                            self._validate_gc_path(root, require_existing=True)
                            self._validate_gc_path(directory, require_existing=True)
                            if (
                                directory.is_symlink()
                                or not directory.is_dir()
                                or _is_storage_link_or_reparse(directory)
                            ):
                                continue
                            shutil.rmtree(directory, ignore_errors=True)
                            removed += 1
                    except (OSError, ClipUploadError):
                        continue
            return removed
        finally:
            lock.release()

    def opportunistic_gc(
        self,
        user_id: Any | None = None,
        *,
        max_age_seconds: float | None = None,
        max_entries: int | None = None,
        time_budget_seconds: float | None = None,
        protected_upload_ids: Iterable[Any] | None = None,
    ) -> int:
        """Best-effort cleanup suitable for upload/ingest request paths.

        The authenticated user's tree is checked on every call.  A bounded
        global sweep runs at a configurable interval so stale uploads from
        inactive users are eventually reclaimed without requiring a dedicated
        background worker.  Lock contention simply skips cleanup and never
        fails the request.
        """

        ttl = self.staging_ttl_seconds() if max_age_seconds is None else max(
            0.0, float(max_age_seconds)
        )
        budget = (
            _DEFAULT_GC_BUDGET_SECONDS
            if time_budget_seconds is None
            else max(0.001, float(time_budget_seconds))
        )
        entries = (
            _DEFAULT_GC_MAX_ENTRIES
            if max_entries is None
            else max(1, int(max_entries))
        )
        # ``protected_upload_ids`` may be a one-shot iterable supplied by a
        # durable job query.  Materialize it once because a user and a global
        # sweep can both run in this call.
        protected_values: list[str] = []
        for value in protected_upload_ids or ():
            try:
                protected_values.append(str(_parse_uuid(value)))
            except ClipUploadError:
                continue
        protected_ids = tuple(protected_values)
        # Keep each request's synchronous work bounded.  A user sweep and a
        # global sweep share the budget rather than doubling it.
        removed = self.gc_staging(
            max_age_seconds=ttl,
            user_id=user_id,
            max_entries=max(1, entries // 2) if user_id is not None else entries,
            time_budget_seconds=budget / 2 if user_id is not None else budget,
            protected_upload_ids=protected_ids,
        )
        key = str(self.workspace_root)
        now = time.monotonic()
        interval = self._env_float(
            "AOITALK_DOCS_CLIP_STAGING_GLOBAL_GC_INTERVAL_SECONDS",
            _DEFAULT_GLOBAL_GC_INTERVAL_SECONDS,
            minimum=0.0,
        )
        with self._gc_locks_guard:
            previous = self._last_global_gc.get(key, 0.0)
            should_run_global = now - previous >= interval
            if should_run_global and not protected_ids:
                # Claim the interval before scanning to avoid a thundering
                # herd.  A scan that loses the lock is harmless and the next
                # request will retry after the interval.
                self._last_global_gc[key] = now
        # A request-scoped protection set is normally built from all active
        # durable jobs.  Do not launch the opportunistic global sweep when the
        # set is non-empty: an older caller may only have supplied a subset,
        # and a global pass would otherwise race active jobs owned by other
        # users.  The explicit maintenance ``gc_staging`` API remains
        # available to callers that can provide a complete protection set.
        if should_run_global and user_id is not None and not protected_ids:
            removed += self.gc_staging(
                max_age_seconds=ttl,
                max_entries=max(1, entries // 2),
                time_budget_seconds=budget / 2,
                protected_upload_ids=protected_ids,
            )
        return removed


__all__ = ["ClipIngestStorage", "ClipUpload", "ClipUploadError"]
