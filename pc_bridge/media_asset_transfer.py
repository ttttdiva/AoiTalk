"""Bounded media staging for one authenticated PC Bridge connection.

This module never opens a browser, selects a file input, or publishes. Wire
commands accept opaque IDs and bytes, never server or caller filesystem paths.
The client must destroy the store on disconnect; reconnect creates a new epoch.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from .paths import data_dir

CHUNK_BYTES = 256 * 1024  # Base64 + JSON fit the 1 MiB native-message boundary.
IMAGE_MAX_BYTES = 25 * 1024 * 1024
VIDEO_MAX_BYTES = 512 * 1024 * 1024
YOUTUBE_PREPARE_MAX_BYTES = 64 * 1024 * 1024
MAX_TRANSFERS = 4
MAX_TOTAL_BYTES = VIDEO_MAX_BYTES
MAX_AGE_SECONDS = 30 * 60
MIME_EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "image/gif": ".gif", "video/mp4": ".mp4", "video/webm": ".webm",
    "video/quicktime": ".mov",
}
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMON = {"transfer_id", "scope_hash", "connection_epoch"}


class MediaAssetTransferError(RuntimeError):
    """Fixed error codes only: no paths, bytes, or credentials in responses."""

    def __init__(self, code, *, outcome_unknown=False, transfer_id=None):
        self.code, self.outcome_unknown, self.transfer_id = code, outcome_unknown, transfer_id
        super().__init__(code)


@dataclass
class _Transfer:
    transfer_id: str
    scope_hash: str
    sha256: str
    size_bytes: int
    mime_type: str
    path: Path = field(repr=False)
    fd: int = field(repr=False)
    identity: tuple = field(repr=False)
    created_at: float = field(default_factory=time.monotonic)
    offset: int = 0
    state: str = "receiving"
    chunks: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class LocalVerifiedMediaAsset:
    """Internal native-upload authority; this object must never become a DTO."""
    transfer_id: str
    path: Path = field(repr=False)
    sha256: str
    size_bytes: int
    mime_type: str


def _identity(info):
    # Python 3.12 Windows lstat reports creation time in st_ctime while
    # fstat reports change time. Compare the explicit creation time there.
    timestamp = getattr(info, "st_birthtime_ns", info.st_ctime_ns) if os.name == "nt" else info.st_ctime_ns
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, timestamp


def _no_links(path):
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise MediaAssetTransferError("bridge_asset_path_untrusted")


def _format_matches(mime, head):
    if mime == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if mime == "video/webm":
        return head.startswith(b"\x1aE\xdf\xa3") and b"webm" in head
    return (len(head) >= 16 and head[4:8] == b"ftyp"
            and int.from_bytes(head[:4], "big") >= 16
            and (head[8:12] == b"qt  ") == (mime == "video/quicktime"))


class MediaAssetTransferStore:
    """One WSS epoch owns a private, bounded temporary staging directory."""

    def __init__(self, *, device_id: str, connection_epoch: str | None = None,
                 root: Path | None = None, browser=None, cancel_event=None):
        if not isinstance(device_id, str) or not device_id or len(device_id) > 128:
            raise MediaAssetTransferError("bridge_asset_device_invalid")
        epoch = connection_epoch or uuid4().hex
        if not isinstance(epoch, str) or not _ID.fullmatch(epoch):
            raise MediaAssetTransferError("bridge_asset_epoch_invalid")
        self.device_id, self.connection_epoch = device_id, epoch
        self._browser = browser
        self._cancel_event = cancel_event
        self._lock = threading.RLock()
        self._records: dict[str, _Transfer] = {}
        self._retired: set[str] = set()
        self._closed = False
        root = Path(root) if root is not None else data_dir() / "media-transfers"
        if not root.is_absolute():
            raise MediaAssetTransferError("bridge_asset_path_untrusted")
        _no_links(root)
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _no_links(root)
            self._directory = Path(tempfile.mkdtemp(prefix="connection-", dir=root))
            _no_links(self._directory)
        except OSError:
            raise MediaAssetTransferError("bridge_asset_storage_unavailable") from None

    def _receipt(self, record):
        return {"transfer_id": record.transfer_id, "scope_hash": record.scope_hash,
                "connection_epoch": self.connection_epoch, "sha256": record.sha256,
                "size_bytes": record.size_bytes, "mime_type": record.mime_type,
                "state": record.state, "offset": record.offset,
                "upload_available": False}

    def _remove(self, record):
        self._records.pop(record.transfer_id, None)
        self._retired.add(record.transfer_id)
        try:
            os.close(record.fd)
        except OSError:
            pass
        try:
            record.path.unlink(missing_ok=True)
        except OSError:
            pass

    def _check_file(self, record):
        try:
            _no_links(self._directory)
            actual = record.path.lstat()
            held = os.fstat(record.fd)
            if (not stat.S_ISREG(actual.st_mode)
                    or _identity(actual) != _identity(held)
                    or actual.st_nlink != 1
                    or (held.st_dev, held.st_ino) != record.identity):
                raise MediaAssetTransferError("bridge_asset_changed")
            return held
        except OSError:
            raise MediaAssetTransferError("bridge_asset_changed") from None

    def _lookup(self, params):
        transfer_id = params.get("transfer_id")
        if not isinstance(transfer_id, str) or not _ID.fullmatch(transfer_id):
            raise MediaAssetTransferError("bridge_asset_id_invalid")
        record = self._records.get(transfer_id)
        if record is None:
            raise MediaAssetTransferError("bridge_asset_transfer_unknown")
        if (params.get("connection_epoch") != self.connection_epoch
                or params.get("scope_hash") != record.scope_hash):
            raise MediaAssetTransferError("bridge_asset_scope_mismatch")
        if time.monotonic() - record.created_at > MAX_AGE_SECONDS:
            self._remove(record)
            raise MediaAssetTransferError("bridge_asset_transfer_expired")
        self._check_file(record)
        return record

    def dispatch(self, action: str, params: dict) -> dict:
        with self._lock:
            if self._closed or (self._cancel_event is not None and self._cancel_event.is_set()):
                raise MediaAssetTransferError("bridge_asset_connection_closed")
            if not isinstance(params, dict):
                raise MediaAssetTransferError("bridge_asset_params_invalid")
            try:
                if action == "begin":
                    return self._begin(params)
                if action == "prepare_x_image":
                    return self._prepare_x_image(params)
                if action == "prepare_youtube_video":
                    return self._prepare_youtube_video(params)
                allowed = {"append": _COMMON | {"offset", "data", "chunk_sha256"},
                           "finish": _COMMON, "discard": _COMMON}
                if action not in allowed or set(params) != allowed[action]:
                    raise MediaAssetTransferError("bridge_asset_params_invalid")
                record = self._lookup(params)
                if action == "discard":
                    self._remove(record)
                    return {"transfer_id": record.transfer_id, "state": "discarded"}
                if action == "append":
                    return self._append(record, params)
                return self._finish(record)
            except OSError:
                raise MediaAssetTransferError("bridge_asset_storage_unavailable") from None

    def _begin(self, params):
        if set(params) != {"transfer_id", "scope_hash", "sha256", "size_bytes", "mime_type"}:
            raise MediaAssetTransferError("bridge_asset_params_invalid")
        transfer_id, scope_hash, sha = (params[name] for name in ("transfer_id", "scope_hash", "sha256"))
        size, mime = params["size_bytes"], params["mime_type"]
        if (not isinstance(transfer_id, str) or not _ID.fullmatch(transfer_id)
                or not isinstance(scope_hash, str) or not _SHA.fullmatch(scope_hash)
                or not isinstance(sha, str) or not _SHA.fullmatch(sha)
                or not isinstance(mime, str) or mime not in MIME_EXTENSIONS
                or type(size) is not int or not 0 < size <= (
                    IMAGE_MAX_BYTES if mime.startswith("image/") else VIDEO_MAX_BYTES)):
            raise MediaAssetTransferError("bridge_asset_manifest_invalid")
        prior = self._records.get(transfer_id)
        if prior:
            if (prior.scope_hash, prior.sha256, prior.size_bytes, prior.mime_type) != (scope_hash, sha, size, mime):
                raise MediaAssetTransferError("bridge_asset_manifest_changed")
            return self._receipt(self._lookup({"transfer_id": transfer_id,
                "scope_hash": scope_hash, "connection_epoch": self.connection_epoch}))
        if transfer_id in self._retired or len(self._retired) >= 4096:
            raise MediaAssetTransferError("bridge_asset_id_retired")
        # Expired files are owned by this store only; unrelated data is untouched.
        for record in list(self._records.values()):
            if time.monotonic() - record.created_at > MAX_AGE_SECONDS:
                self._remove(record)
        if len(self._records) >= MAX_TRANSFERS or sum(r.size_bytes for r in self._records.values()) + size > MAX_TOTAL_BYTES:
            raise MediaAssetTransferError("bridge_asset_staging_limit")
        path = self._directory / (transfer_id + MIME_EXTENSIONS[mime])
        _no_links(self._directory)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
        info = os.fstat(fd)
        record = _Transfer(transfer_id, scope_hash, sha, size, mime, path, fd, (info.st_dev, info.st_ino))
        self._records[transfer_id] = record
        return self._receipt(record)

    def _append(self, record, params):
        offset, encoded, chunk_sha = params["offset"], params["data"], params["chunk_sha256"]
        if (type(offset) is not int or offset < 0 or not isinstance(encoded, str)
                or not 0 < len(encoded) <= ((CHUNK_BYTES + 2) // 3) * 4
                or not isinstance(chunk_sha, str) or not _SHA.fullmatch(chunk_sha)):
            raise MediaAssetTransferError("bridge_asset_chunk_invalid")
        try:
            block = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise MediaAssetTransferError("bridge_asset_chunk_invalid") from None
        if not 0 < len(block) <= CHUNK_BYTES or not hmac.compare_digest(hashlib.sha256(block).hexdigest(), chunk_sha):
            raise MediaAssetTransferError("bridge_asset_chunk_hash_mismatch")
        if offset < record.offset:
            # Only the same accepted chunk can be acknowledged twice. Neither
            # caller nor transport may treat this as permission to replay UI input.
            if record.chunks.get(offset) != (len(block), chunk_sha):
                raise MediaAssetTransferError("bridge_asset_chunk_conflict")
            os.lseek(record.fd, offset, os.SEEK_SET)
            if os.read(record.fd, len(block)) != block:
                raise MediaAssetTransferError("bridge_asset_changed")
            return self._receipt(record)
        if record.state != "receiving" or offset != record.offset or offset + len(block) > record.size_bytes:
            raise MediaAssetTransferError("bridge_asset_chunk_out_of_order")
        if os.fstat(record.fd).st_size != record.offset:
            raise MediaAssetTransferError("bridge_asset_changed")
        os.lseek(record.fd, offset, os.SEEK_SET)
        view = memoryview(block)
        while view:
            written = os.write(record.fd, view)
            if written <= 0:
                raise MediaAssetTransferError("bridge_asset_storage_unavailable")
            view = view[written:]
        record.chunks[offset] = (len(block), chunk_sha)
        record.offset += len(block)
        self._check_file(record)
        return self._receipt(record)

    def _verify(self, record):
        before = self._check_file(record)
        if before.st_size != record.size_bytes or record.offset != record.size_bytes:
            raise MediaAssetTransferError("bridge_asset_incomplete")
        digest, head, total = hashlib.sha256(), b"", 0
        os.lseek(record.fd, 0, os.SEEK_SET)
        while block := os.read(record.fd, CHUNK_BYTES):
            total += len(block)
            if total > record.size_bytes:
                raise MediaAssetTransferError("bridge_asset_changed")
            if not head:
                head = block[:512]
            digest.update(block)
        if (total != record.size_bytes or not hmac.compare_digest(digest.hexdigest(), record.sha256)
                or _identity(before) != _identity(self._check_file(record))):
            raise MediaAssetTransferError("bridge_asset_hash_mismatch")
        if not _format_matches(record.mime_type, head):
            raise MediaAssetTransferError("bridge_asset_mime_mismatch")

    def _finish(self, record):
        if record.state not in {"receiving", "ready"}:
            raise MediaAssetTransferError("bridge_asset_already_consumed")
        try:
            self._verify(record)
        except MediaAssetTransferError as exc:
            if str(exc) != "bridge_asset_incomplete":
                self._remove(record)
            raise
        os.fsync(record.fd)
        record.state = "ready"
        return self._receipt(record)

    @contextmanager
    def consume_for_upload(self, *, transfer_id, scope_hash, connection_epoch):
        """Local trusted adapter only, one use before dispatch, never a wire path.

        On an exception the transfer stays consumed. The receipt authority must
        record the unknown outcome rather than initiate another upload.
        """
        with self._lock:
            if self._closed or (self._cancel_event is not None and self._cancel_event.is_set()):
                raise MediaAssetTransferError("bridge_asset_connection_closed")
            record = self._lookup({"transfer_id": transfer_id, "scope_hash": scope_hash,
                                   "connection_epoch": connection_epoch})
            if record.state != "ready":
                raise MediaAssetTransferError("bridge_asset_not_ready")
            self._verify(record)
            record.state = "consumed"
            yield LocalVerifiedMediaAsset(record.transfer_id, record.path, record.sha256,
                                          record.size_bytes, record.mime_type)

    def close(self):
        with self._lock:
            self._closed = True
            for record in list(self._records.values()):
                self._remove(record)
            # This random directory belongs exclusively to this connection.
            shutil.rmtree(self._directory, ignore_errors=True)

    def _prepare_x_image(self, params):
        return self._prepare_asset(params, platform="x", hash_field="text_sha256",
            mime_types={"image/png", "image/jpeg", "image/webp"}, max_bytes=IMAGE_MAX_BYTES,
            command_prefix="media_prepare_asset_")

    def _prepare_youtube_video(self, params):
        return self._prepare_asset(params, platform="youtube", hash_field="metadata_sha256",
            mime_types={"video/mp4", "video/webm"}, max_bytes=YOUTUBE_PREPARE_MAX_BYTES,
            command_prefix="media_prepare_youtube_asset_")

    def _prepare_asset(self, params, *, platform, hash_field, mime_types, max_bytes, command_prefix):
        pins = {"tab_id", "run_id", "expected_document_id", "expected_account_ref", hash_field,
                "expected_browser_context"}
        if (set(params) != _COMMON | pins or type(params.get("tab_id")) is not int
                or params["tab_id"] < 0
                or not isinstance(params.get("run_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", params["run_id"])
                or not isinstance(params.get("expected_document_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", params["expected_document_id"])
                or not isinstance(params.get("expected_account_ref"), str)
                or not re.fullmatch(r"@[a-z0-9_]{1,15}" if platform == "x" else r"UC[A-Za-z0-9_-]{22}", params["expected_account_ref"])
                or not isinstance(params.get(hash_field), str) or not _SHA.fullmatch(params[hash_field])):
            raise MediaAssetTransferError("bridge_asset_prepare_params_invalid")
        context = params["expected_browser_context"]
        try:
            if (not isinstance(context, dict) or set(context) != {"context_id", "context_epoch"}
                    or any(not isinstance(value, str) or str(UUID(value)) != value or UUID(value).int == 0 for value in context.values())):
                raise ValueError()
        except ValueError:
            raise MediaAssetTransferError("bridge_asset_prepare_params_invalid") from None
        context = dict(context)
        record = self._lookup(params)
        if record.mime_type not in mime_types:
            raise MediaAssetTransferError("bridge_asset_prepare_mime_unsupported")
        if record.size_bytes > max_bytes:
            raise MediaAssetTransferError("bridge_asset_prepare_size_unsupported")
        if self._browser is None:
            raise MediaAssetTransferError("bridge_asset_prepare_unavailable")
        fixed = {key: params[key] for key in pins}
        fixed["expected_browser_context"] = dict(context)
        fixed.update(platform=platform, scope_hash=record.scope_hash, transfer_id=record.transfer_id)
        expected = {"transfer_id": record.transfer_id, "scope_hash": record.scope_hash,
                    "document_id": params["expected_document_id"],
                    "account_ref": params["expected_account_ref"], "asset_sha256": record.sha256}

        def call(command, command_params, offset, *, final=False):
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise MediaAssetTransferError("bridge_asset_connection_closed",
                    outcome_unknown=True, transfer_id=record.transfer_id)
            try:
                result = self._browser._request(command,
                    {**command_params, "expected_browser_context": dict(context)})
            except Exception:
                raise MediaAssetTransferError("bridge_asset_prepare_outcome_unknown",
                    outcome_unknown=True, transfer_id=record.transfer_id) from None
            fields = set(expected) | {"offset", "accepted", "dispatched", "files_verified",
                                     "attachment_visible", "executed", "outcome_unknown"}
            valid = (isinstance(result, dict) and set(result) in (fields, fields | {"error"})
                and all(result.get(key) == value for key, value in expected.items())
                and type(result.get("offset")) is int and result["offset"] == offset
                and all(type(result.get(key)) is bool for key in (
                    "accepted", "dispatched", "files_verified", "attachment_visible", "executed", "outcome_unknown"))
                and ("error" not in result or (isinstance(result["error"], str)
                    and re.fullmatch(r"media_prepare_[a-z_]+", result["error"]))))
            if not valid:
                raise MediaAssetTransferError("bridge_asset_prepare_receipt_invalid",
                    outcome_unknown=True, transfer_id=record.transfer_id)
            if result.get("error") or result["outcome_unknown"] or not result["accepted"]:
                raise MediaAssetTransferError("bridge_asset_prepare_rejected",
                    outcome_unknown=result["outcome_unknown"] or result["executed"], transfer_id=record.transfer_id)
            if final:
                if not (result["dispatched"] and result["executed"] and result["files_verified"] and result["attachment_visible"]):
                    raise MediaAssetTransferError("bridge_asset_attachment_unverified",
                        outcome_unknown=result["executed"], transfer_id=record.transfer_id)
            elif any(result[key] for key in ("dispatched", "executed", "files_verified", "attachment_visible")):
                raise MediaAssetTransferError("bridge_asset_prepare_receipt_invalid",
                    outcome_unknown=True, transfer_id=record.transfer_id)
            return result

        # Consume before any browser IPC. Even a lost begin reply cannot be
        # replayed under this transfer ID. No command clicks a publish button.
        with self.consume_for_upload(**{key: params[key] for key in _COMMON}):
            call(command_prefix + "begin", {**fixed, "asset_sha256": record.sha256,
                "size_bytes": record.size_bytes, "mime_type": record.mime_type}, 0)
            os.lseek(record.fd, 0, os.SEEK_SET)
            offset, digest = 0, hashlib.sha256()
            while block := os.read(record.fd, CHUNK_BYTES):
                if offset + len(block) > record.size_bytes:
                    raise MediaAssetTransferError("bridge_asset_changed")
                call(command_prefix + "append", {**fixed, "offset": offset,
                    "base64": base64.b64encode(block).decode("ascii")}, offset + len(block))
                digest.update(block)
                offset += len(block)
            self._verify(record)
            if offset != record.size_bytes or not hmac.compare_digest(digest.hexdigest(), record.sha256):
                raise MediaAssetTransferError("bridge_asset_changed")
            return call(command_prefix + "finish", fixed, offset, final=True)
