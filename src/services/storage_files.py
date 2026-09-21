"""Files operations scoped to one explicitly registered root.

These endpoints do not reuse the legacy admin-absolute-path escape hatch.
All client paths are logical POSIX relative paths. Root directories are never
created. Uploads are bounded chunks, staged on their destination storage, and
published atomically; deleting a file moves it to that storage's trash.
"""
from __future__ import annotations

import json
import mimetypes
import os
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from .storage_io import StorageError, StorageUnavailable, check_io_cancelled
from .storage_roots import INTERNAL_DIR, StorageRoot, assert_no_links, is_link, relative_parts

CHUNK_BYTES = 4 * 1024 * 1024
TEXT_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 20_000


def _etag(info: os.stat_result) -> str:
    return f"{info.st_ino:x}-{info.st_size:x}-{info.st_mtime_ns:x}"


def _existing(root: StorageRoot, path: str, *, write: bool = False, internal: bool = False) -> Path:
    item = root.resolve(path, write=write, internal=internal)
    try:
        info = item.lstat()
    except FileNotFoundError as exc:
        root.require_online(write=write)
        raise StorageError("file_not_found", "ファイルが見つかりません", 404) from exc
    if is_link(info) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise StorageError("unsafe_file", "通常のファイル・ディレクトリだけを利用できます", 403)
    return item


def _internal(root: StorageRoot, relative: str) -> Path:
    return root.resolve(f"{INTERNAL_DIR}/{relative}", write=True, internal=True)


def _ensure_internal_directory(root: StorageRoot, relative: str) -> Path:
    cursor = INTERNAL_DIR
    for component in (None, *relative_parts(relative, internal=True)):
        if component is not None:
            cursor += "/" + component
        path = root.resolve(cursor, write=True, internal=True)
        check_io_cancelled()
        try:
            path.mkdir()  # Never parents=True: do not recreate a missing root.
        except FileExistsError:
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise StorageError("storage_metadata_conflict", "管理用ディレクトリが利用できません", 409)
        assert_no_links(path)
    return path


@contextmanager
def _write_lock(root: StorageRoot) -> Iterator[None]:
    _ensure_internal_directory(root, "")
    lock = _internal(root, "write.lock")
    deadline = time.monotonic() + 1.0
    while True:
        check_io_cancelled()
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise StorageError("storage_busy", "ストレージで別の更新処理が実行中です", 409)
            time.sleep(0.02)
    try:
        yield
    finally:
        # A lost mount must never cause cleanup at an unmounted local path.
        try:
            root.require_online(write=True)
            assert_no_links(lock)
            lock.unlink(missing_ok=True)
        except (OSError, StorageError):
            pass


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    assert_no_links(temporary, allow_missing=True)
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        check_io_cancelled()
        assert_no_links(path, allow_missing=True)
        os.replace(temporary, path)
    finally:
        try:
            assert_no_links(temporary, allow_missing=True)
            temporary.unlink(missing_ok=True)
        except (OSError, StorageError):
            pass


def list_files(root: StorageRoot, path: str = "") -> dict:
    directory = _existing(root, path)
    if not directory.is_dir():
        raise StorageError("not_a_directory", "ディレクトリではありません", 400)
    entries = []
    skipped = 0
    truncated = False
    with os.scandir(directory) as scan:
        for entry in scan:
            check_io_cancelled()
            if entry.name == INTERNAL_DIR or entry.name.startswith(".aoitalk-storage-"):
                continue
            rel = f"{path}/{entry.name}" if path else entry.name
            try:
                relative_parts(rel)
                info = entry.stat(follow_symlinks=False)
                if is_link(info) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    skipped += 1
                    continue
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                entries.append({
                    "name": entry.name, "path": rel,
                    "is_directory": stat.S_ISDIR(info.st_mode),
                    "size_bytes": info.st_size if stat.S_ISREG(info.st_mode) else None,
                    "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                    "etag": _etag(info),
                    "mime_type": mimetypes.guess_type(entry.name)[0] or "application/octet-stream",
                })
            except (FileNotFoundError, StorageError):
                skipped += 1
    root.require_online()
    entries.sort(key=lambda item: (not item["is_directory"], item["name"].casefold(), item["name"]))
    return {"entries": entries, "path": path, "parent_path": "/".join(relative_parts(path)[:-1]) if path else None,
            "skipped_entries": skipped, "truncated": truncated}


def open_download(root: StorageRoot, path: str) -> tuple[BinaryIO, os.stat_result, str]:
    target = _existing(root, path)
    if not target.is_file():
        raise StorageError("not_a_file", "通常のファイルを指定してください", 400)
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise StorageError("unsafe_file", "通常のファイルを指定してください", 403)
        # Check both the lexical path and the root identity again after opening.
        root.resolve(path)
        check_io_cancelled()
        return os.fdopen(descriptor, "rb"), info, mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    except BaseException:
        os.close(descriptor)
        raise


def read_text(root: StorageRoot, path: str) -> dict:
    handle, info, _ = open_download(root, path)
    with handle:
        if info.st_size > TEXT_BYTES:
            raise StorageError("text_too_large", "エディタで開けるのは2MiBまでです。ダウンロードを利用してください", 413)
        raw = handle.read(TEXT_BYTES + 1)
    root.require_online()
    if len(raw) > TEXT_BYTES:
        raise StorageError("text_too_large", "ファイルが大きすぎます", 413)
    try:
        content = raw.decode("utf-8")
        if "\x00" in content:
            raise UnicodeError()
    except UnicodeError as exc:
        raise StorageError("not_text", "UTF-8テキストではありません。ダウンロードを利用してください", 415) from exc
    return {"path": path, "content": content, "etag": _etag(info)}


def _check_destination(root: StorageRoot, path: str, expected_etag: str | None) -> Path:
    if not relative_parts(path):
        raise StorageError("protected_root", "ストレージroot自体は変更できません", 403)
    destination = root.resolve(path, write=True)
    parent_rel = "/".join(relative_parts(path)[:-1])
    parent = _existing(root, parent_rel, write=True)
    if not parent.is_dir():
        raise StorageError("not_a_directory", "保存先の親ディレクトリがありません", 400)
    try:
        info = destination.lstat()
    except FileNotFoundError:
        if expected_etag is not None:
            raise StorageError("file_conflict", "ファイルの状態が変わりました。再読み込みしてください", 409)
    else:
        if not stat.S_ISREG(info.st_mode) or expected_etag is None or _etag(info) != expected_etag:
            raise StorageError("file_conflict", "同名ファイルがあるか、他の操作で変更されています", 409)
    return destination


def save_text(root: StorageRoot, path: str, content: str, etag: str | None) -> dict:
    raw = content.encode("utf-8")
    if len(raw) > TEXT_BYTES:
        raise StorageError("text_too_large", "2MiBを超えるテキストは保存できません", 413)
    with _write_lock(root):
        destination = _check_destination(root, path, etag)
        temporary = destination.with_name(f".aoitalk-storage-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            check_io_cancelled()
            _check_destination(root, path, etag)
            os.replace(temporary, destination)
        finally:
            try:
                root.require_online(write=True)
                assert_no_links(temporary, allow_missing=True)
                temporary.unlink(missing_ok=True)
            except (OSError, StorageError):
                pass
        return {"path": path, "etag": _etag(destination.stat())}


def mkdir(root: StorageRoot, path: str) -> dict:
    if not relative_parts(path):
        raise StorageError("protected_root", "ストレージroot自体は変更できません", 403)
    with _write_lock(root):
        destination = root.resolve(path, write=True)
        check_io_cancelled()
        try:
            destination.mkdir()
        except FileExistsError as exc:
            raise StorageError("file_conflict", "同名のファイルまたはフォルダがあります", 409) from exc
        return {"path": path}


def move(root: StorageRoot, source: str, destination: str) -> dict:
    if not relative_parts(source) or not relative_parts(destination):
        raise StorageError("protected_root", "ストレージroot自体は変更できません", 403)
    if destination.startswith(source + "/"):
        raise StorageError("invalid_path", "移動元の配下には移動できません", 400)
    with _write_lock(root):
        src = _existing(root, source, write=True)
        dst = root.resolve(destination, write=True)
        if dst.exists():
            raise StorageError("file_conflict", "移動先に同名のファイルがあります", 409)
        check_io_cancelled()
        # All source/destination paths belong to this one root. Cross-root moves
        # are deliberately not simulated as a destructive copy+delete.
        os.rename(src, dst)
        return {"path": destination}


def trash(root: StorageRoot, path: str, owner: str) -> dict:
    if not relative_parts(path):
        raise StorageError("protected_root", "ストレージroot自体は変更できません", 403)
    with _write_lock(root):
        source = _existing(root, path, write=True)
        tid = uuid.uuid4().hex
        directory = _ensure_internal_directory(root, f"trash/{tid}")
        _write_json(directory / "metadata.json", {"path": path, "owner": owner})
        check_io_cancelled()
        root.resolve(path, write=True)
        os.rename(source, directory / "item")
        return {"trash_id": tid, "path": path}


def _transaction_id(value: str) -> str:
    if not isinstance(value, str) or not re_full_uuid(value):
        raise StorageError("invalid_id", "無効な操作idです", 400)
    return value


def re_full_uuid(value: str) -> bool:
    try:
        return len(value) == 32 and uuid.UUID(value).hex == value
    except (ValueError, AttributeError):
        return False


def _metadata(root: StorageRoot, path: str, owner: str, admin: bool = False) -> dict:
    target = _existing(root, f"{INTERNAL_DIR}/{path}", write=True, internal=True)
    if target.stat().st_size > 32 * 1024:
        raise StorageError("invalid_metadata", "管理情報が無効です", 409)
    try:
        item = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise StorageError("invalid_metadata", "管理情報が無効です", 409) from exc
    if not isinstance(item, dict) or (not admin and item.get("owner") != owner):
        raise StorageError("file_not_found", "操作情報が見つかりません", 404)
    relative_parts(item.get("path"))
    return item


def restore(root: StorageRoot, tid: str, owner: str, admin: bool = False) -> dict:
    tid = _transaction_id(tid)
    with _write_lock(root):
        item = _metadata(root, f"trash/{tid}/metadata.json", owner, admin)
        destination = root.resolve(item["path"], write=True)
        if destination.exists():
            raise StorageError("file_conflict", "復元先に同名のファイルがあります", 409)
        source = _existing(root, f"{INTERNAL_DIR}/trash/{tid}/item", write=True, internal=True)
        check_io_cancelled()
        os.rename(source, destination)
        return {"path": item["path"]}


def start_upload(root: StorageRoot, path: str, size: int, owner: str, etag: str | None = None) -> dict:
    maximum = int(os.environ.get("AOITALK_STORAGE_MAX_FILE_BYTES", str(1024 ** 4)))
    if type(size) is not int or size < 0 or size > maximum:
        raise StorageError("upload_too_large", "アップロードサイズが上限を超えています", 413)
    with _write_lock(root):
        _check_destination(root, path, etag)
        uid = uuid.uuid4().hex
        directory = _ensure_internal_directory(root, "uploads")
        metadata = {"path": path, "size": size, "owner": owner, "etag": etag,
                    "created_at": datetime.now(timezone.utc).isoformat()}
        with (directory / f"{uid}.part").open("xb"):
            pass
        _write_json(directory / f"{uid}.json", metadata)
        return {"upload_id": uid, "received": 0, "chunk_bytes": CHUNK_BYTES}


def upload_status(root: StorageRoot, uid: str, owner: str) -> dict:
    uid = _transaction_id(uid)
    metadata = _metadata(root, f"uploads/{uid}.json", owner)
    part = _existing(root, f"{INTERNAL_DIR}/uploads/{uid}.part", write=True, internal=True)
    return {"upload_id": uid, "received": part.stat().st_size, "size": metadata["size"], "chunk_bytes": CHUNK_BYTES}


def upload_chunk(root: StorageRoot, uid: str, owner: str, offset: int, content: bytes) -> dict:
    uid = _transaction_id(uid)
    if not content or len(content) > CHUNK_BYTES or offset < 0:
        raise StorageError("invalid_upload_chunk", "アップロードchunkが無効です", 400)
    with _write_lock(root):
        metadata = _metadata(root, f"uploads/{uid}.json", owner)
        part = _existing(root, f"{INTERNAL_DIR}/uploads/{uid}.part", write=True, internal=True)
        received = part.stat().st_size
        if received != offset:
            raise StorageError("upload_offset_conflict", f"受信済みサイズは{received}bytesです。状態を再取得してください", 409)
        if received + len(content) > metadata["size"]:
            raise StorageError("upload_too_large", "宣言されたファイルサイズを超えています", 413)
        check_io_cancelled()
        descriptor = os.open(part, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return {"upload_id": uid, "received": received + len(content)}


def finish_upload(root: StorageRoot, uid: str, owner: str) -> dict:
    uid = _transaction_id(uid)
    with _write_lock(root):
        metadata = _metadata(root, f"uploads/{uid}.json", owner)
        source = _existing(root, f"{INTERNAL_DIR}/uploads/{uid}.part", write=True, internal=True)
        if source.stat().st_size != metadata["size"]:
            raise StorageError("upload_incomplete", "アップロードが完了していません", 409)
        destination = _check_destination(root, metadata["path"], metadata["etag"])
        check_io_cancelled()
        os.replace(source, destination)
        _internal(root, f"uploads/{uid}.json").unlink(missing_ok=True)
        return {"path": metadata["path"], "etag": _etag(destination.stat())}


def cancel_upload(root: StorageRoot, uid: str, owner: str) -> dict:
    uid = _transaction_id(uid)
    with _write_lock(root):
        _metadata(root, f"uploads/{uid}.json", owner)
        check_io_cancelled()
        for suffix in ("part", "json"):
            _internal(root, f"uploads/{uid}.{suffix}").unlink(missing_ok=True)
        return {"cancelled": True}
