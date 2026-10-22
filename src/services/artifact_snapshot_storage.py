"""Bounded Files snapshots in the existing Artifact CAS; no additional ledger.

Chunk hashes are persisted in immutable Artifact provenance so a Range download
can verify only the requested chunks, before yielding them, without a full read.
"""

from __future__ import annotations

import hashlib
import asyncio
import os
import threading
from dataclasses import dataclass
from uuid import uuid4
from pathlib import Path
from .generated_media_storage import MediaRoot

from .media_asset_service import _identity
from .media_asset_storage import open_asset, pinned_parent, remove_asset
from .storage_io import StorageError, check_io_cancelled, storage_io
from .storage_roots import StorageRoot, read_registry

CHUNK_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 1024**3
INTEGRITY_KEY = "artifact_stream_v1"
STAGE_DIRECTORY = ".aoitalk-storage-product"


def product_file_limit():
    try:
        configured = int(os.environ.get("AOITALK_STORAGE_MAX_FILE_BYTES", str(1024**4)))
    except ValueError:
        raise StorageError(
            "invalid_storage_limit", "Filesのサイズ上限設定が無効です", 503
        ) from None
    if configured < 1:
        raise StorageError(
            "invalid_storage_limit", "Filesのサイズ上限設定が無効です", 503
        )
    return min(MAX_SNAPSHOT_BYTES, configured)


def artifact_root():
    from .operations_service import _artifact_storage_root

    # Reuse enrolled mount identity on every I/O, including staging cleanup.
    configured = os.environ.get("AOITALK_OPERATIONS_ARTIFACT_ROOT", "").strip()
    selected = (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parents[2] / "data" / "operations_artifacts"
    )
    mount = read_registry().containing(selected)
    if mount:
        mount.require_online(write=True)
    path = _artifact_storage_root()
    return MediaRoot(
        id="operations-artifacts",
        name="Artifact CAS",
        root_path=str(path),
        external=False,
        mount=mount,
    )


def staging_root(root):
    # Files excludes and forbids this managed prefix. Pending bytes are never a
    # selectable/downloadable Files object, even on a registered shared mount.
    with pinned_parent(root, "stage-directory-probe", write=True) as (
        descriptor,
        parent,
        _,
    ):
        try:
            if descriptor is None:
                (parent / STAGE_DIRECTORY).mkdir(mode=0o700)
            else:
                os.mkdir(STAGE_DIRECTORY, 0o700, dir_fd=descriptor)
        except FileExistsError:
            pass
    path = root.resolve(STAGE_DIRECTORY, write=True, internal=True)
    return MediaRoot(
        id="artifact-staging",
        name="Artifact staging",
        root_path=str(path),
        external=False,
        mount=root,
    )


@dataclass(frozen=True)
class ArtifactSnapshot:
    root: StorageRoot
    sha256: str
    size_bytes: int
    chunks: tuple[str, ...]
    identity: tuple

    @property
    def integrity(self):
        return {"chunk_bytes": CHUNK_BYTES, "chunks": list(self.chunks)}


def source_identity(root, relative):
    with open_asset(root, relative) as (_, info):
        return _identity(info)


def read_source_chunk(root, relative, identity, offset, count):
    with open_asset(root, relative) as (handle, info):
        if _identity(info) != identity:
            raise StorageError(
                "file_changed", "Filesの実体が変更されました。再選択してください", 409
            )
        handle.seek(offset)
        data = handle.read(count)
        if (
            _identity(os.fstat(handle.fileno())) != identity
            or source_identity(root, relative) != identity
        ):
            raise StorageError(
                "file_changed", "Filesの実体が読み取り中に変更されました", 409
            )
        if len(data) != count:
            raise StorageError("file_changed", "Filesの読み取りが中断されました", 409)
        check_io_cancelled()
        return data


def verify_cas(root, relative, size, digest, chunks):
    with open_asset(root, relative) as (handle, info):
        identity = _identity(info)
        if info.st_size != size:
            raise StorageError(
                "artifact_changed", "Artifactのサイズが一致しません", 409
            )
        observed = hashlib.sha256()
        for pin in chunks:
            check_io_cancelled()
            data = handle.read(CHUNK_BYTES)
            if hashlib.sha256(data).hexdigest() != pin:
                raise StorageError(
                    "artifact_changed", "Artifactのchunk hashが一致しません", 409
                )
            observed.update(data)
        if (
            handle.read(1)
            or observed.hexdigest() != digest
            or source_identity(root, relative) != identity
        ):
            raise StorageError("artifact_changed", "Artifactの実体が一致しません", 409)
        check_io_cancelled()
        return identity


async def copy_snapshot(
    source_root, relative, identity, *, validate_source, disconnected=None
):
    size = identity[2]
    if not 0 < size <= product_file_limit():
        raise StorageError(
            "product_file_too_large",
            "配布物は1 GiB以下かつFilesの設定上限以下を指定してください",
            413,
        )
    root = await storage_io.run("operations-artifacts", artifact_root, mutation=True)
    stage_root = await storage_io.run(
        root.io_key, lambda: staging_root(root), mutation=True
    )
    stage = f"{uuid4().hex}.tmp"
    inode = None
    stage_worker_active = threading.Event()

    def remove_stage():
        remove_asset(stage_root, stage, expected_inode=inode)

    async def stage_io(function, **kwargs):
        def work():
            stage_worker_active.set()
            try:
                return function()
            finally:
                # function has exited all open_asset/pinned_parent contexts.
                stage_worker_active.clear()

        return await storage_io.run(
            root.io_key, work, on_abandoned=remove_stage, **kwargs
        )

    def create_stage():
        # Own the pinned parent through create and its cancellation cleanup.
        # A timed-out worker may finish later, after its awaiting task has ended.
        with pinned_parent(stage_root, stage, write=True) as (descriptor, parent, name):
            created = None
            try:
                fd = os.open(
                    name if descriptor is not None else parent / name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    **({"dir_fd": descriptor} if descriptor is not None else {}),
                )
                try:
                    info = os.fstat(fd)
                    created = info.st_dev, info.st_ino
                finally:
                    os.close(fd)
                check_io_cancelled()
                return created
            except BaseException:
                if created is not None:
                    current = os.stat(
                        name if descriptor is not None else parent / name,
                        follow_symlinks=False,
                        **({"dir_fd": descriptor} if descriptor is not None else {}),
                    )
                    if (current.st_dev, current.st_ino) == created:
                        os.unlink(
                            name if descriptor is not None else parent / name,
                            **(
                                {"dir_fd": descriptor} if descriptor is not None else {}
                            ),
                        )
                raise

    creation = asyncio.create_task(
        storage_io.run(root.io_key, create_stage, mutation=True)
    )
    try:
        # Cancellation cannot abandon a completed stage before its inode reaches
        # the caller. Finally awaits the same creation, then removes that inode.
        inode = await asyncio.shield(creation)
        digest, chunks, offset = hashlib.sha256(), [], 0
        while offset < size:
            if disconnected is not None and await disconnected():
                raise StorageError(
                    "product_import_cancelled", "配布物の取込を中止しました", 409
                )
            data = await storage_io.run(
                source_root.io_key,
                lambda: read_source_chunk(
                    source_root,
                    relative,
                    identity,
                    offset,
                    min(CHUNK_BYTES, size - offset),
                ),
            )

            def append():
                with open_asset(
                    stage_root,
                    stage,
                    flags=os.O_WRONLY | os.O_APPEND,
                    expected_inode=inode,
                ) as (handle, _):
                    handle.write(data)
                    check_io_cancelled()

            await stage_io(append, mutation=True)
            digest.update(data)
            chunks.append(hashlib.sha256(data).hexdigest())
            offset += len(data)
        sha = digest.hexdigest()
        stage_identity = await stage_io(
            lambda: verify_cas(stage_root, stage, size, sha, chunks), timeout=120
        )
        await validate_source()
        if disconnected is not None and await disconnected():
            raise StorageError(
                "product_import_cancelled", "配布物の取込を中止しました", 409
            )

        def publish():
            if source_identity(stage_root, stage) != stage_identity:
                raise StorageError(
                    "artifact_changed", "Artifact stagingが変更されました", 409
                )
            with open_asset(
                stage_root, stage, flags=os.O_WRONLY | os.O_APPEND, expected_inode=inode
            ) as (handle, _):
                handle.flush()
                os.fsync(handle.fileno())
            with (
                pinned_parent(stage_root, stage, write=True) as (
                    source_fd,
                    source_parent,
                    name,
                ),
                pinned_parent(root, sha, write=True) as (
                    target_fd,
                    target_parent,
                    target,
                ),
            ):
                check_io_cancelled()
                try:
                    if source_fd is None:
                        os.rename(source_parent / name, target_parent / target)
                    else:
                        os.link(
                            name,
                            target,
                            src_dir_fd=source_fd,
                            dst_dir_fd=target_fd,
                            follow_symlinks=False,
                        )
                        os.unlink(name, dir_fd=source_fd)
                except FileExistsError:
                    pass
            # An existing CAS object is shared: verify it, never overwrite/delete.
            return verify_cas(root, sha, size, sha, chunks)

        final_identity = await stage_io(publish, timeout=120, mutation=True)
        return ArtifactSnapshot(root, sha, size, tuple(chunks), final_identity)
    finally:
        if inode is None:
            try:
                inode = await asyncio.shield(creation)
            except (Exception, asyncio.CancelledError):
                # A StorageIO timeout makes create_stage clean up in its worker.
                inode = None
        if inode is not None and not stage_worker_active.is_set():
            # An active abandoned worker owns the late cleanup after closing its
            # handles (Windows denies deletion while open). Otherwise cleanup is
            # safe now. Both paths delete only our inode, never shared CAS bytes.
            await asyncio.shield(
                storage_io.run(
                    root.io_key,
                    remove_stage,
                    mutation=True,
                )
            )


async def prepare_snapshot(artifact):
    import re

    digest, size = str(artifact.sha256), artifact.size_bytes
    if (
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        or type(size) is not int
        or not 0 < size <= MAX_SNAPSHOT_BYTES
    ):
        raise StorageError("artifact_invalid", "Artifactの固定版情報が無効です", 409)
    if artifact.storage_ref != f"operations_artifacts/{digest}":
        raise StorageError("artifact_invalid", "Artifactの保存参照が一致しません", 409)
    root = await storage_io.run("operations-artifacts", artifact_root)
    identity = await storage_io.run(root.io_key, lambda: source_identity(root, digest))
    if identity[2] != size:
        raise StorageError("artifact_changed", "Artifactのサイズが一致しません", 409)
    integrity = (artifact.provenance_json or {}).get(INTEGRITY_KEY)
    if integrity is None and size <= 25 * 1024 * 1024:
        # Legacy small Artifact registrations have no chunk pins. Verify them
        # with bounded reads before deriving pins; large snapshots always have pins.
        def legacy():
            chunks, observed = [], hashlib.sha256()
            for offset in range(0, size, CHUNK_BYTES):
                data = read_source_chunk(
                    root, digest, identity, offset, min(CHUNK_BYTES, size - offset)
                )
                chunks.append(hashlib.sha256(data).hexdigest())
                observed.update(data)
            if observed.hexdigest() != digest:
                raise StorageError(
                    "artifact_changed", "Artifactのhashが一致しません", 409
                )
            return chunks

        chunks = await storage_io.run(root.io_key, legacy, timeout=120)
    else:
        if (
            not isinstance(integrity, dict)
            or integrity.get("chunk_bytes") != CHUNK_BYTES
            or not isinstance(integrity.get("chunks"), list)
            or len(integrity["chunks"]) != (size + CHUNK_BYTES - 1) // CHUNK_BYTES
            or any(
                not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin)
                for pin in integrity["chunks"]
            )
        ):
            raise StorageError(
                "artifact_invalid", "Artifactのstream検証情報が無効です", 409
            )
        chunks = integrity["chunks"]
    return ArtifactSnapshot(root, digest, size, tuple(chunks), identity)


async def stream_snapshot(snapshot, start, end):
    first, last = start // CHUNK_BYTES, end // CHUNK_BYTES
    for index in range(first, last + 1):
        offset = index * CHUNK_BYTES
        data = await storage_io.run(
            snapshot.root.io_key,
            lambda: read_source_chunk(
                snapshot.root,
                snapshot.sha256,
                snapshot.identity,
                offset,
                min(CHUNK_BYTES, snapshot.size_bytes - offset),
            ),
        )
        if hashlib.sha256(data).hexdigest() != snapshot.chunks[index]:
            raise StorageError(
                "artifact_changed", "配信中のArtifactのhashが一致しません", 409
            )
        yield data[max(0, start - offset) : min(len(data), end - offset + 1)]
