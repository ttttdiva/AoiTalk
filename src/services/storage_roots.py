"""Explicit additional Files roots. PostgreSQL, Qdrant and workspaces stay local.

The registry is local configuration, not user content. Merely loading/probing it
never creates a storage root. Mounted roots require an explicitly enrolled
identity file so an unmounted directory cannot become a writable local fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterator

from .storage_io import StorageError, StorageUnavailable, check_io_cancelled

MAX_ROOTS = 32
MAX_CONFIG_BYTES = 256 * 1024
INTERNAL_DIR = ".aoitalk-storage"
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_REGISTRY_LOCK = threading.RLock()


def lexical_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def is_link(item: os.stat_result) -> bool:
    return stat.S_ISLNK(item.st_mode) or bool(
        getattr(item, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def assert_no_links(path: Path, *, allow_missing: bool = False) -> Path:
    """Inspect every component with lstat (including dangling links and root)."""
    path = lexical_path(path)
    cursor = Path(path.anchor)
    # Inspect the anchor too: mapped drives/UNC roots must be accessible.
    for part in (None, *path.parts[1:]):
        if part is not None:
            cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            if allow_missing:
                return path
            raise
        if is_link(info):
            raise StorageError("unsafe_storage_path", "symlink / junction / reparse point は利用できません", 403)
        junction = getattr(cursor, "is_junction", None)
        if callable(junction) and junction():
            raise StorageError("unsafe_storage_path", "junction は利用できません", 403)
    return path


def relative_parts(value: str, *, internal: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str) or len(value) > 4096:
        raise StorageError("invalid_path", "無効な相対パスです", 400)
    if not value:
        return ()
    if "\\" in value or value.startswith("/") or PureWindowsPath(value).drive:
        raise StorageError("invalid_path", "絶対パスは利用できません", 400)
    parts = tuple(value.split("/"))
    for part in parts:
        if (part in {"", ".", ".."} or part.endswith((" ", "."))
            or any(ord(c) < 32 or ord(c) == 127 for c in part)
            or any(c in part for c in ':*?"<>|')
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED):
            raise StorageError("invalid_path", "無効な相対パスです", 400)
        if not internal and (part.casefold() in {".git", ".trash", INTERNAL_DIR}
                             or part.casefold().startswith(".aoitalk-storage-")):
            raise StorageError("protected_path", "ストレージ管理用パスにはアクセスできません", 403)
    # Reject normalization rather than silently changing the requested target.
    if PurePosixPath(value).parts != parts:
        raise StorageError("invalid_path", "無効な相対パスです", 400)
    return parts


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class StorageRoot:
    id: str
    name: str
    root_path: str
    read_only: bool = False
    enabled: bool = True
    external: bool = True
    project_ids: tuple[str, ...] = ()
    user_ids: tuple[str, ...] = ()
    shared: bool = False
    identity: str = ""

    @property
    def path(self) -> Path:
        return lexical_path(self.root_path)

    @property
    def marker_name(self) -> str:
        return f".aoitalk-storage-{self.id}.identity"

    @property
    def io_key(self) -> str:
        # Serialize aliases for the same physical root even across API clients.
        return f"storage:{os.path.normcase(str(self.path))}"

    @classmethod
    def parse(cls, value: object, *, identity: str | None = None) -> StorageRoot:
        if not isinstance(value, dict):
            raise ValueError("root は object で指定してください")
        allowed = set(cls.__dataclass_fields__)
        if set(value) - allowed:
            raise ValueError("未知のストレージ設定項目があります")
        sid = value.get("id")
        if not isinstance(sid, str) or not _ID.fullmatch(sid) or sid == "default":
            raise ValueError("id は小文字英数字・ハイフン・アンダースコアで指定してください")
        name = value.get("name")
        path = value.get("root_path")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("表示名は1～120文字で指定してください")
        if not isinstance(path, str) or not path or "\x00" in path or not Path(path).expanduser().is_absolute():
            raise ValueError("root_path はホストOSの絶対パスで指定してください")
        booleans = {}
        for field, default in (("read_only", False), ("enabled", True), ("external", True), ("shared", False)):
            item = value.get(field, default)
            if type(item) is not bool:
                raise ValueError(f"{field} は boolean で指定してください")
            booleans[field] = item
        memberships = {}
        for field in ("project_ids", "user_ids"):
            items = value.get(field, [])
            if not isinstance(items, (list, tuple)) or len(items) > 256:
                raise ValueError(f"{field} はUUIDの配列で指定してください")
            memberships[field] = tuple(sorted({str(uuid.UUID(str(i))) for i in items}))
        marker = identity if identity is not None else value.get("identity", "")
        if marker and (not isinstance(marker, str) or str(uuid.UUID(marker)) != marker):
            raise ValueError("identity が無効です")
        if booleans["external"] and not marker:
            raise ValueError("外部ストレージには identity が必要です")
        return cls(sid, name.strip(), str(lexical_path(path)), **booleans,
                   **memberships, identity=marker or "")

    def public(self, *, admin: bool = False) -> dict[str, object]:
        item: dict[str, object] = {
            "id": self.id, "name": self.name, "read_only": self.read_only,
            "enabled": self.enabled, "external": self.external,
            "project_ids": list(self.project_ids), "online": None,
            "status": "checking" if self.enabled else "disabled",
            "configuration_revision": hashlib.sha256(
                json.dumps(asdict(self), sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
        if admin:
            item.update(root_path=self.root_path, user_ids=list(self.user_ids),
                        shared=self.shared, marker_name=self.marker_name,
                        identity=self.identity)
        return item

    def require_online(self, *, write: bool = False) -> Path:
        if not self.enabled:
            raise StorageUnavailable("ストレージは無効化されています")
        if write and self.read_only:
            raise StorageError("storage_read_only", "このストレージは読み取り専用です", 403)
        try:
            root = assert_no_links(self.path)
            if not stat.S_ISDIR(root.lstat().st_mode):
                raise StorageUnavailable("ストレージrootがディレクトリではありません")
            if self.external:
                marker = assert_no_links(root / self.marker_name)
                info = marker.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > 128:
                    raise StorageUnavailable("ストレージの識別ファイルが無効です")
                with marker.open("r", encoding="ascii") as handle:
                    if handle.read(129).strip() != self.identity:
                        raise StorageUnavailable("接続されたストレージの識別情報が一致しません")
            return root
        except StorageError:
            raise
        except (OSError, UnicodeError, RuntimeError) as exc:
            raise StorageUnavailable("ストレージが利用できません。接続と識別ファイルを確認してください") from exc

    def resolve(self, value: str, *, write: bool = False, internal: bool = False) -> Path:
        parts = relative_parts(value, internal=internal)
        root = self.require_online(write=write)
        candidate = root.joinpath(*parts)
        try:
            assert_no_links(candidate, allow_missing=True)
        except OSError as exc:
            self.require_online(write=write)
            raise StorageUnavailable() from exc
        if not _under(candidate, root):
            raise StorageError("invalid_path", "root外へのアクセスは禁止されています", 403)
        return candidate

    def enroll(self) -> None:
        """Explicit admin operation only. Never called by startup/list/read/write."""
        if not self.external:
            return
        # The read-only flag governs Files operations; this separately requested
        # administrative operation writes ONLY this identity file. OS-RO mounts
        # need the documented marker provisioned by their storage administrator.
        try:
            root = assert_no_links(self.path)
            if not root.is_dir():
                raise StorageUnavailable()
            marker = root / self.marker_name
            assert_no_links(marker, allow_missing=True)
            check_io_cancelled()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(marker, flags, 0o600)
            except FileExistsError:
                self.require_online()
                return
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(self.identity + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except StorageError:
            raise
        except OSError as exc:
            raise StorageUnavailable("識別ファイルを作成できません。OS側の接続・書き込み権限を確認してください") from exc


@dataclass(frozen=True)
class RegistrySnapshot:
    roots: tuple[StorageRoot, ...]
    revision: str

    def get(self, root_id: str) -> StorageRoot:
        for root in self.roots:
            if root.id == root_id:
                return root
        raise StorageError("storage_not_found", "ストレージが見つかりません", 404)

    def containing(self, path: Path) -> StorageRoot | None:
        path = lexical_path(path)
        return next((root for root in self.roots if _under(path, root.path)), None)


def registry_path() -> Path:
    return lexical_path(os.environ.get("AOITALK_STORAGE_ROOTS_FILE") or "data/storage_roots.json")


def _validate_roots(roots: tuple[StorageRoot, ...]) -> None:
    if len(roots) > MAX_ROOTS or len({r.id for r in roots}) != len(roots):
        raise ValueError("ストレージidの重複または登録数上限超過です")
    workspace = lexical_path(os.environ.get("AOITALK_WORKSPACES_DIR") or "./workspaces")
    config = registry_path()
    for index, root in enumerate(roots):
        if _under(root.path, workspace) or _under(workspace, root.path):
            raise ValueError("既存workspacesと重なるrootは登録できません")
        if _under(config, root.path):
            raise ValueError("設定ファイルを含むrootは登録できません")
        for other in roots[:index]:
            if _under(root.path, other.path) or _under(other.path, root.path):
                raise ValueError("他の登録済みrootと重なるパスは利用できません")


def read_registry() -> RegistrySnapshot:
    path = registry_path()
    try:
        assert_no_links(path, allow_missing=True)
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            raw = b""
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("設定ファイルが大きすぎます")
        content = json.loads(raw) if raw else {"version": 1, "roots": []}
        if not isinstance(content, dict) or content.get("version") != 1 or not isinstance(content.get("roots"), list):
            raise ValueError("ストレージ設定の形式が無効です")
        roots = tuple(StorageRoot.parse(item) for item in content["roots"])
        _validate_roots(roots)
        return RegistrySnapshot(roots, hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError, TypeError, StorageError) as exc:
        raise StorageError("storage_configuration_error", "ストレージ設定を読み込めません。ローカルFilesは引き続き利用できます", 503) from exc


@contextmanager
def _configuration_lock(path: Path) -> Iterator[None]:
    """Cross-process writer admission; abandoned locks fail closed (not broken)."""
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + 3.0
    with _REGISTRY_LOCK:
        assert_no_links(path.parent, allow_missing=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        assert_no_links(path.parent)
        while True:
            try:
                fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise StorageError("storage_configuration_busy", "ストレージ設定を更新中です", 409)
                time.sleep(0.02)
        try:
            os.close(fd)
            yield
        finally:
            lock.unlink(missing_ok=True)


def save_root(value: dict[str, object], *, expected_revision: str) -> RegistrySnapshot:
    path = registry_path()
    with _configuration_lock(path):
        current = read_registry()
        if current.revision != expected_revision:
            raise StorageError("storage_configuration_conflict", "設定が更新されています。再読み込みしてください", 409)
        previous = next((item for item in current.roots if item.id == value.get("id")), None)
        # Identity is server-assigned and never accepted from a normal editor.
        identity = previous.identity if previous and previous.identity else str(uuid.uuid4())
        try:
            root = StorageRoot.parse(value, identity=identity)
            roots = tuple(item for item in current.roots if item.id != root.id) + (root,)
            _validate_roots(roots)
        except (ValueError, TypeError) as exc:
            raise StorageError("invalid_storage_configuration", str(exc), 400) from exc
        raw = (json.dumps({"version": 1, "roots": [asdict(item) for item in roots]},
                          ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".storage-roots-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            check_io_cancelled()
            assert_no_links(path, allow_missing=True)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return read_registry()
