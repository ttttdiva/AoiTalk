"""Transfer a formally verified media snapshot to one owner-bound Bridge.

This is an internal preparation adapter, not an upload or publication receipt.
Its caller retains the Bridge operation lock for the whole preparation task.
There is no public-network or server-local browser fallback, and no retry.
"""
from __future__ import annotations

import base64
import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import UUID, uuid4

from pc_bridge.media_asset_transfer import (
    CHUNK_BYTES, IMAGE_MAX_BYTES, VIDEO_MAX_BYTES, MIME_EXTENSIONS, YOUTUBE_PREPARE_MAX_BYTES,
)
from .media_asset_service import MediaAssetService


class MediaBridgeAssetTransferError(RuntimeError):
    def __init__(self, code, *, transfer_id=None, outcome_unknown=False):
        self.code, self.transfer_id, self.outcome_unknown = code, transfer_id, outcome_unknown
        super().__init__(code)


@dataclass(frozen=True)
class MediaBridgeAssetTransferReceipt:
    transfer_id: str
    scope_hash: str
    connection_epoch: str
    sha256: str
    size_bytes: int
    mime_type: str
    state: str = "ready"
    upload_available: bool = False

    def to_safe_dict(self):
        return dict(vars(self))


class MediaBridgeAssetTransfer:
    def __init__(self, bridge, *, asset_service=None, scratch_root=None):
        self.bridge = bridge
        self.assets = asset_service or MediaAssetService()
        self.scratch_root = Path(scratch_root) if scratch_root is not None else (
            Path(__file__).resolve().parents[2] / ".local" / "media-bridge-assets")

    async def _call(self, action, params, transfer_id):
        try:
            self.bridge._check_connection()
            return await self.bridge.hub.call(
                self.bridge.owner, self.bridge.device_id, "media_asset", action,
                params, expected_connection=self.bridge._connection,
            )
        except asyncio.CancelledError as exc:
            # Preserve cancellation semantics while giving the durable caller
            # the opaque staging identity needed for its uncertain receipt.
            exc.transfer_id = transfer_id
            exc.outcome_unknown = True
            raise
        except Exception:
            # A timeout/lost reply may already have written PC staging bytes.
            # Preserve that ambiguity; never retry or use a replacement PC.
            raise MediaBridgeAssetTransferError(
                "bridge_asset_transfer_unknown", transfer_id=transfer_id,
                outcome_unknown=True,
            ) from None

    def _reply(self, reply, expected, *, epoch=None, state, offset):
        allowed = set(expected) | {"connection_epoch", "state", "offset", "upload_available"}
        if (not isinstance(reply, dict) or set(reply) != allowed
                or any(reply.get(key) != value for key, value in expected.items())
                or reply.get("state") != state or type(reply.get("offset")) is not int
                or reply["offset"] != offset or reply.get("upload_available") is not False
                or not isinstance(reply.get("connection_epoch"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", reply["connection_epoch"])
                or (epoch is not None and reply["connection_epoch"] != epoch)):
            raise MediaBridgeAssetTransferError("bridge_asset_receipt_invalid",
                transfer_id=expected["transfer_id"], outcome_unknown=True)
        return reply["connection_epoch"]

    async def transfer(self, session, generation_output_id, *, owner_user_id,
                       project_id, expected_sha256, scope_hash):
        if str(owner_user_id) != self.bridge.owner:
            raise MediaBridgeAssetTransferError("bridge_asset_owner_mismatch")
        if (not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                or not isinstance(scope_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", scope_hash)):
            raise MediaBridgeAssetTransferError("bridge_asset_manifest_invalid")
        self.bridge._check_connection()
        if "media_asset" not in self.bridge._connection.capabilities:
            raise MediaBridgeAssetTransferError("bridge_asset_capability_unavailable")
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        transfer_id = uuid4().hex
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=self.scratch_root) as directory:
            target = Path(directory) / "asset"
            asset = await self.assets.materialize_for_provider(
                session, generation_output_id, owner_user_id=owner_user_id,
                project_id=project_id, expected_sha256=expected_sha256, target=target,
            )
            if asset is None:
                raise MediaBridgeAssetTransferError("bridge_asset_managed_binding_required")
            if (Path(asset.path) != target or asset.mime_type not in MIME_EXTENSIONS
                    or type(asset.size_bytes) is not int
                    or not isinstance(asset.sha256, str)
                    or not 0 < asset.size_bytes <= (IMAGE_MAX_BYTES if asset.mime_type.startswith("image/") else VIDEO_MAX_BYTES)
                    or not hmac.compare_digest(asset.sha256, expected_sha256)):
                raise MediaBridgeAssetTransferError("bridge_asset_snapshot_invalid")
            manifest = {"transfer_id": transfer_id, "scope_hash": scope_hash,
                "sha256": asset.sha256, "size_bytes": asset.size_bytes, "mime_type": asset.mime_type}
            # Read only the private, newly materialized file through a no-links
            # descriptor. The server filename never enters a wire parameter.
            try:
                fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_BINARY", 0))
            except OSError:
                raise MediaBridgeAssetTransferError("bridge_asset_snapshot_invalid") from None
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != asset.size_bytes:
                    raise MediaBridgeAssetTransferError("bridge_asset_snapshot_invalid")
                reply = await self._call("begin", manifest, transfer_id)
                epoch = self._reply(reply, manifest, state="receiving", offset=0)
                binding = {"transfer_id": transfer_id, "scope_hash": scope_hash, "connection_epoch": epoch}
                total, digest = 0, hashlib.sha256()
                while block := os.read(fd, CHUNK_BYTES):
                    if total + len(block) > asset.size_bytes:
                        raise MediaBridgeAssetTransferError("bridge_asset_snapshot_changed", transfer_id=transfer_id)
                    params = {**binding, "offset": total, "data": base64.b64encode(block).decode("ascii"),
                              "chunk_sha256": hashlib.sha256(block).hexdigest()}
                    reply = await self._call("append", params, transfer_id)
                    total += len(block)
                    digest.update(block)
                    self._reply(reply, manifest, epoch=epoch, state="receiving", offset=total)
                after = os.fstat(fd)
                # Windows lstat/fstat differ in st_ctime meaning; explicit birthtime
                # preserves a comparable creation-time check without discarding it.
                identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                    getattr(info, "st_birthtime_ns", info.st_ctime_ns) if os.name == "nt" else info.st_ctime_ns)
                if (total != asset.size_bytes or not hmac.compare_digest(digest.hexdigest(), asset.sha256)
                        or identity(before) != identity(after) or identity(after) != identity(target.lstat())):
                    raise MediaBridgeAssetTransferError("bridge_asset_snapshot_changed", transfer_id=transfer_id)
                reply = await self._call("finish", binding, transfer_id)
                self._reply(reply, manifest, epoch=epoch, state="ready", offset=total)
                return MediaBridgeAssetTransferReceipt(transfer_id, scope_hash, epoch,
                    asset.sha256, asset.size_bytes, asset.mime_type)
            finally:
                os.close(fd)

    async def prepare_x_image(self, receipt: MediaBridgeAssetTransferReceipt, *,
                              tab_id, run_id, expected_document_id,
                              expected_account_ref, text_sha256):
        """Prepare an attachment on the same verified text run, no final click.

        This method returns attachment evidence only; platform submission and
        publication still require separate human confirmation and receipts.
        """
        return await self._prepare_asset(receipt, "prepare_x_image",
            {"image/png", "image/jpeg", "image/webp"}, IMAGE_MAX_BYTES,
            tab_id=tab_id, run_id=run_id, expected_document_id=expected_document_id,
            expected_account_ref=expected_account_ref, text_sha256=text_sha256)

    async def prepare_youtube_video(self, receipt: MediaBridgeAssetTransferReceipt, *,
                                   tab_id, run_id, expected_document_id,
                                   expected_account_ref, metadata_sha256):
        """Stage a video in the existing Studio UI, without its final Save.

        The initial DOM capability is limited to 64 MiB MP4/WebM. Positive
        attachment evidence does not claim the remote upload has completed.
        The caller must separately verify all approved metadata in the UI.
        """
        return await self._prepare_asset(receipt, "prepare_youtube_video",
            {"video/mp4", "video/webm"}, YOUTUBE_PREPARE_MAX_BYTES,
            tab_id=tab_id, run_id=run_id, expected_document_id=expected_document_id,
            expected_account_ref=expected_account_ref, metadata_sha256=metadata_sha256)

    async def _prepare_asset(self, receipt, action, mime_types, max_bytes, **pins):
        if (not isinstance(receipt, MediaBridgeAssetTransferReceipt)
                or receipt.state != "ready" or receipt.upload_available is not False
                or receipt.mime_type not in mime_types
                or type(receipt.size_bytes) is not int
                or not 0 < receipt.size_bytes <= max_bytes):
            raise MediaBridgeAssetTransferError("bridge_asset_prepare_manifest_invalid")
        context = getattr(self.bridge, "context", None)
        try:
            if (not isinstance(context, dict) or set(context) != {"context_id", "context_epoch"}
                    or any(not isinstance(value, str) or str(UUID(value)) != value or UUID(value).int == 0 for value in context.values())):
                raise ValueError()
        except ValueError:
            raise MediaBridgeAssetTransferError("bridge_asset_browser_context_unverified",
                transfer_id=receipt.transfer_id) from None
        context = dict(context)
        params = {"transfer_id": receipt.transfer_id, "scope_hash": receipt.scope_hash,
                  "connection_epoch": receipt.connection_epoch, **pins,
                  "expected_browser_context": context}
        result = await self._call(action, params, receipt.transfer_id)
        expected = {"transfer_id": receipt.transfer_id, "scope_hash": receipt.scope_hash,
                    "document_id": pins["expected_document_id"], "account_ref": pins["expected_account_ref"],
                    "asset_sha256": receipt.sha256, "offset": receipt.size_bytes}
        fields = set(expected) | {"accepted", "dispatched", "files_verified", "attachment_visible", "executed", "outcome_unknown"}
        if (not isinstance(result, dict) or set(result) != fields
                or any(result.get(key) != value for key, value in expected.items())
                or any(result.get(key) is not True for key in (
                    "accepted", "dispatched", "files_verified", "attachment_visible", "executed"))
                or result.get("outcome_unknown") is not False):
            raise MediaBridgeAssetTransferError("bridge_asset_attachment_receipt_invalid",
                transfer_id=receipt.transfer_id, outcome_unknown=True)
        return result
