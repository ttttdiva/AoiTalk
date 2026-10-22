"""Owner-scoped media snapshots using the existing generated-media storage.

Public commands accept IDs only. Filesystem locations and Studio credentials
remain internal. Every preview/provider read verifies the immutable binding
against a freshly loaded output and the actual bytes, without ORM cache trust.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections import deque
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    GeneratedMedia,
    GenerationOutput,
    GenerationRun,
    GenerationRunIntent,
    GenerationWorkspace,
    Persona,
    ContentItem,
    EditorialProgram,
)
from ..memory.models.media_assets import MediaAssetRevision
from .generated_media_storage import get_media_root, media_relative_path
from .generation_studio_auth import (
    GenerationStudioTokenProvider,
    GenerationStudioUnavailableError,
    generation_studio_token_provider,
)
from .media_operations_content_service import MediaOperationsContentService
from .media_operations_service import (
    MediaOperationsAuthorizationError,
    MediaOperationsConflictError,
    MediaOperationsNotFoundError,
    MediaOperationsValidationError,
    _actor_id,
    sha256_json,
)
from .media_asset_storage import (
    make_asset_directory,
    open_asset,
    publish_asset,
    remove_asset,
)
from .storage_io import StorageError, check_io_cancelled, storage_io
from .storage_files import list_files
from ..memory.project_repository import ProjectRepository
from .storage_roots import (
    StorageRoot,
    lexical_path,
    read_registry,
    relative_parts,
)

CHUNK_BYTES = 1024 * 1024
IMAGE_MAX_BYTES = 25 * 1024 * 1024
VIDEO_MAX_BYTES = 512 * 1024 * 1024
LIBRARY_MAX_ENTRIES = 20_000
MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


class MediaAssetError(MediaOperationsValidationError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code = code
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class VerifiedMediaAsset:
    generation_output_id: UUID
    path: Path = field(repr=False)
    sha256: str
    mime_type: str
    size_bytes: int


def _mime(value: str) -> str:
    value = str(value or "").strip().lower()
    if value not in MIME_EXTENSIONS:
        raise MediaAssetError(
            "asset_mime_unsupported",
            "PNG/JPEG/WebP/GIF または MP4/WebM/QuickTime を指定してください",
        )
    return value


def _sha(value: str | None) -> str | None:
    if value is not None and not re.fullmatch(r"[0-9a-f]{64}", str(value)):
        raise MediaAssetError("asset_hash_invalid", "SHA256 が無効です")
    return value


def _limit(mime: str) -> int:
    return IMAGE_MAX_BYTES if mime.startswith("image/") else VIDEO_MAX_BYTES


def _storage_unavailable(exc: StorageError) -> bool:
    return exc.status_code == 503 and exc.code in {
        "storage_unavailable",
        "storage_busy",
    }


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    if os.name == "nt":
        # Python 3.12 exposes creation time separately on Windows.  st_ctime_ns
        # can describe a metadata-change timestamp on a descriptor while the
        # path stat still reports the file birth time after an append.
        timestamp_ns = getattr(info, "st_birthtime_ns", None)
        if timestamp_ns is None:
            # Do not fall back to st_ctime_ns: comparing an unstable timestamp
            # weakens the replacement check, while accepting an absent field
            # would silently turn the identity check into a false allow.
            raise OSError("Windows file identity lacks st_birthtime_ns")
    else:
        timestamp_ns = info.st_ctime_ns
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, timestamp_ns


def _verify_file(
    root: StorageRoot,
    relative: str,
    mime: str,
    expected: str | None = None,
    size: int | None = None,
):
    """Bounded hash + actual format verification on a no-links descriptor."""
    try:
        with open_asset(root, relative) as (handle, before):
            if before.st_size <= 0 or before.st_size > _limit(mime):
                raise MediaAssetError(
                    "asset_size_invalid",
                    "素材が空か、アップロード上限を超えています",
                    413,
                )
            digest = hashlib.sha256()
            head = b""
            total = 0
            while block := handle.read(CHUNK_BYTES):
                check_io_cancelled()
                if not head:
                    head = block[:512]
                total += len(block)
                if total > _limit(mime):
                    raise MediaAssetError(
                        "asset_too_large", "素材がアップロード上限を超えています", 413
                    )
                digest.update(block)
            observed = digest.hexdigest()
            if (size is not None and total != size) or (
                expected is not None and not hmac.compare_digest(observed, expected)
            ):
                raise MediaAssetError(
                    "asset_hash_mismatch",
                    "素材の SHA256 / size が登録時と一致しません",
                    409,
                )
            if mime.startswith("image/"):
                from PIL import Image

                handle.seek(0)
                try:
                    with Image.open(handle) as image:
                        wanted = {
                            "image/png": "PNG",
                            "image/jpeg": "JPEG",
                            "image/webp": "WEBP",
                            "image/gif": "GIF",
                        }[mime]
                        if (
                            image.format != wanted
                            or max(image.size) > 16384
                            or image.width * image.height > 64_000_000
                        ):
                            raise ValueError()
                        image.verify()
                except Exception as exc:
                    raise MediaAssetError(
                        "asset_format_invalid", "画像の実形式 / サイズが無効です"
                    ) from exc
            elif mime == "video/webm":
                if not head.startswith(b"\x1aE\xdf\xa3") or b"webm" not in head:
                    raise MediaAssetError(
                        "asset_format_invalid", "WebM の実形式が無効です"
                    )
            else:
                if (
                    len(head) < 16
                    or head[4:8] != b"ftyp"
                    or int.from_bytes(head[:4], "big") < 16
                ):
                    raise MediaAssetError(
                        "asset_format_invalid", "MP4 / QuickTime の実形式が無効です"
                    )
                if (head[8:12] == b"qt  ") != (mime == "video/quicktime"):
                    raise MediaAssetError(
                        "asset_format_invalid", "動画の MIME と実形式が一致しません"
                    )
            after = os.fstat(handle.fileno())
            current = root.resolve(relative).lstat()
            if _identity(before) != _identity(after) or _identity(before) != _identity(
                current
            ):
                raise MediaAssetError(
                    "asset_changed", "読み取り中に素材が更新されました", 409
                )
            return observed, total, _identity(before)
    except (FileNotFoundError, OSError) as exc:
        raise MediaAssetError(
            "asset_unavailable", "素材ファイルを取得できません", 404
        ) from exc


def _binding(row: MediaAssetRevision) -> dict:
    return {
        "generation_output_id": str(row.generation_output_id),
        "owner_user_id": str(row.owner_user_id),
        "project_id": str(row.project_id) if row.project_id else None,
        "persona_id": str(row.persona_id),
        "content_item_id": str(row.content_item_id) if row.content_item_id else None,
        "source_kind": row.source_kind,
        "source_id": str(row.source_id) if row.source_id else None,
        "sha256": row.sha256,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
    }


def _receipt(row: MediaAssetRevision) -> dict:
    return {
        **_binding(row),
        "asset_revision_id": str(row.id),
        "revision_hash": row.revision_hash,
        "preview_url": f"/api/operations/media/assets/{row.generation_output_id}/preview?expected_sha256={row.sha256}",
        "created_at": row.created_at,
    }


def _asset_command_hash(
    *,
    persona_id,
    content_item_id,
    expected_content_item_hash,
    source_kind,
    source_id,
    sha256,
    mime_type,
    size_bytes,
):
    """Hash the immutable registration command shared by upload and retries."""
    return sha256_json(
        {
            "persona_id": str(persona_id),
            "content_item_id": str(content_item_id) if content_item_id else None,
            "expected_content_item_hash": expected_content_item_hash,
            "source_kind": source_kind,
            "source_id": str(source_id) if source_id else None,
            "sha256": sha256,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
        }
    )


class MediaAssetService(MediaOperationsContentService):
    def __init__(
        self,
        *,
        storage_root: Path | str = "data/generated_media",
        workspace_root: Path | str | None = None,
        http_client_factory=None,
        studio_token_provider: GenerationStudioTokenProvider | None = None,
    ):
        super().__init__()
        self.studio_token_provider = studio_token_provider
        self.storage_root = Path(storage_root)
        self.workspace_root = lexical_path(
            workspace_root or os.getenv("AOITALK_WORKSPACES_DIR", "workspaces")
        )
        self.http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(
                timeout=120, follow_redirects=False, trust_env=False
            )
        )

    def _root(self):
        return get_media_root(self.storage_root)

    async def _execute(self, session, statement):
        # ACL rows must also bypass any identity cached before a long upload.
        return await super()._execute(
            session, statement.execution_options(populate_existing=True)
        )

    async def _live(self, session, model, ident, label="asset"):
        try:
            ident = UUID(str(ident))
        except (ValueError, TypeError, AttributeError):
            raise MediaAssetError("asset_id_invalid", "素材 ID が無効です") from None
        result = await session.execute(
            select(model)
            .where(model.id == ident)
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise MediaOperationsNotFoundError(f"{label} not found")
        return row

    async def _target(
        self, session, actor, persona_id, content_item_id, expected_content_item_hash
    ):
        persona = await self._live(session, Persona, persona_id, "persona")
        await self._assert_entity_access(session, actor, persona, permission="write")
        # Serialize the immutable registration/idempotency command per persona.
        await session.execute(
            select(Persona.id).where(Persona.id == persona.id).with_for_update()
        )
        if content_item_id:
            await session.execute(
                select(ContentItem.id)
                .where(ContentItem.id == UUID(str(content_item_id)))
                .with_for_update()
            )
            item = await self._live(
                session, ContentItem, content_item_id, "content item"
            )
            await self._assert_entity_access(session, actor, item, permission="write")
            program = await self._live(
                session, EditorialProgram, item.editorial_program_id, "program"
            )
            if (item.owner_user_id, item.project_id, program.persona_id) != (
                persona.owner_user_id,
                persona.project_id,
                persona.id,
            ):
                raise MediaOperationsAuthorizationError(
                    "ContentItem と persona の scope が一致しません"
                )
            if (
                expected_content_item_hash is None
                or _sha(expected_content_item_hash) != item.content_hash
            ):
                raise MediaOperationsConflictError(
                    "ContentItem の hash を再確認してください"
                )
        elif expected_content_item_hash is not None:
            raise MediaAssetError(
                "asset_item_required",
                "ContentItem hash には content_item_id が必要です",
            )
        return persona

    def _stage(self, owner, output_id, mime):
        root = self._root()
        relative = f"operations/{owner}/{output_id}{MIME_EXTENSIONS[mime]}"

        def create():
            for path in ("operations", f"operations/{owner}"):
                make_asset_directory(root, path)
            stage = relative + f".{uuid4().hex}.upload"
            with open_asset(
                root, stage, flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL
            ) as (_, info):
                return stage, relative, (info.st_dev, info.st_ino)

        return root, create

    async def _ingest(self, root, stage, stream, mime, inode):
        total = 0
        async for chunk in stream:
            if not isinstance(chunk, bytes):
                raise MediaAssetError(
                    "asset_stream_invalid", "素材の byte stream が無効です"
                )
            total += len(chunk)
            if total > _limit(mime):
                raise MediaAssetError(
                    "asset_too_large", "素材がアップロード上限を超えています", 413
                )
            for offset in range(0, len(chunk), CHUNK_BYTES):
                block = chunk[offset : offset + CHUNK_BYTES]

                def append():
                    check_io_cancelled()
                    with open_asset(
                        root,
                        stage,
                        flags=os.O_WRONLY | os.O_APPEND,
                        expected_inode=inode,
                    ) as (handle, _):
                        handle.write(block)

                await storage_io.run(root.io_key, append, mutation=True)

    async def _remove(self, root, relative, inode=None):
        def remove():
            check_io_cancelled()
            remove_asset(root, relative, expected_inode=inode)

        await storage_io.run(root.io_key, remove, mutation=True)

    async def _library_roots(self, session, actor, persona, unavailable_roots=None):
        """Reuse the Files namespaces and explicitly granted managed roots."""
        await self._assert_entity_access(session, actor, persona, permission="read")
        actor_id = _actor_id(actor)
        workspace = StorageRoot(
            id="workspace",
            name="Workspace",
            root_path=str(self.workspace_root),
            external=False,
        )
        roots = []
        try:
            workspace_exists = await storage_io.run(
                workspace.io_key, lambda: workspace.path.exists()
            )
        except StorageError as exc:
            if unavailable_roots is None or not _storage_unavailable(exc):
                raise
            unavailable_roots.add(workspace.id)
            workspace_exists = False
        if workspace_exists:
            if actor_id == persona.owner_user_id:
                roots.append(
                    (
                        workspace,
                        await ProjectRepository.get_user_storage_path(
                            persona.owner_user_id
                        ),
                    )
                )
            if persona.project_id:
                roots.append(
                    (
                        workspace,
                        await ProjectRepository.get_storage_path(persona.project_id),
                    )
                )
        for root in read_registry().roots:
            if root.enabled and (
                root.shared
                or str(actor_id) in root.user_ids
                or (persona.project_id and str(persona.project_id) in root.project_ids)
            ):
                roots.append((root, ""))
        return roots

    async def _library_entries(
        self, session, actor, persona, limit=100, source_id=None, unavailable_roots=None, file_filter=None
    ):
        """Opaque, scope-bound IDs for the real Files catalog, not text manifests.

        Listing does not hash entire videos. Registration hashes and verifies
        bytes through the pinned source descriptor before making a snapshot.
        """
        wanted = UUID(str(source_id)) if source_id is not None else None
        entries, visited = [], 0
        failed_roots = set()
        for root, namespace in await self._library_roots(
            session, actor, persona, unavailable_roots
        ):
            if root in failed_roots:
                continue
            directories = deque([(namespace, 0)])
            while directories and visited < LIBRARY_MAX_ENTRIES:
                relative, depth = directories.popleft()
                try:
                    listing = await storage_io.run(
                        root.io_key,
                        lambda: list_files(root, relative),
                        timeout=30,
                    )
                except StorageError as exc:
                    if exc.code == "file_not_found":
                        continue
                    if unavailable_roots is not None and _storage_unavailable(exc):
                        # Keep listing other granted roots, but remove any
                        # candidates from the disconnected root. Authorization
                        # and unsafe-path errors always propagate.
                        entries = [entry for entry in entries if entry[1] != root]
                        failed_roots.add(root)
                        unavailable_roots.add(root.id)
                        break
                    raise
                for entry in listing["entries"]:
                    visited += 1
                    if visited > LIBRARY_MAX_ENTRIES:
                        break
                    if entry["is_directory"]:
                        if depth < 32:
                            directories.append((entry["path"], depth + 1))
                        continue
                    mime, size = entry["mime_type"], entry["size_bytes"]
                    if file_filter is not None:
                        if not file_filter(entry):
                            continue
                    elif mime not in MIME_EXTENSIONS or not size or size > _limit(mime):
                        continue
                    ident = uuid5(
                        NAMESPACE_URL,
                        sha256_json(
                            {
                                "namespace": "aoitalk-files-media/v1",
                                "root_id": root.id,
                                "root_identity": root.identity or str(root.path),
                                "owner_user_id": str(persona.owner_user_id),
                                "project_id": str(persona.project_id)
                                if persona.project_id
                                else None,
                                "relative_path": entry["path"],
                            }
                        ),
                    )
                    if wanted is not None and ident != wanted:
                        continue
                    entries.append((ident, root, entry["path"], mime, size))
                    if wanted is not None or len(entries) >= limit:
                        return entries
        return entries

    async def _library(self, session, actor, ident, persona):
        unavailable_roots = set()
        entries = await self._library_entries(
            session,
            actor,
            persona,
            source_id=ident,
            unavailable_roots=unavailable_roots,
        )
        if not entries:
            if unavailable_roots:
                raise StorageError(
                    "storage_unavailable",
                    "選択した Library 素材の保存先を取得できません",
                    503,
                )
            raise MediaOperationsNotFoundError("Library file not found")
        _, root, relative, mime, size = entries[0]
        # KnowledgeDocument.content_hash represents extracted UTF-8 text;
        # it is never the byte SHA of a Files image/video.
        return root, relative, mime, None, size

    async def _source_stream(self, root, relative, mime, expected, size):
        observed, total, identity = await storage_io.run(
            root.io_key,
            lambda: _verify_file(root, relative, mime, expected, size),
            timeout=120,
        )
        offset = 0
        while offset < total:

            def read():
                with open_asset(root, relative) as (handle, info):
                    if _identity(info) != identity:
                        raise MediaAssetError(
                            "asset_changed", "素材が読み取り中に変更されました", 409
                        )
                    handle.seek(offset)
                    block = handle.read(min(CHUNK_BYTES, total - offset))
                    if (
                        _identity(os.fstat(handle.fileno())) != identity
                        or _identity(root.resolve(relative).lstat()) != identity
                    ):
                        raise MediaAssetError(
                            "asset_changed", "素材が読み取り中に変更されました", 409
                        )
                    check_io_cancelled()
                    return block

            block = await storage_io.run(root.io_key, read)
            if not block:
                raise MediaAssetError(
                    "asset_unavailable", "素材の読み取りが中断されました", 409
                )
            offset += len(block)
            yield block

    async def _studio_stream(self, session, row):
        # This is a separate configured service boundary, never a relaxation
        # of the publisher's public HTTPS/SSRF reader.
        run = await self._live(
            session, GenerationRun, row.generation_run_id, "generation run"
        )
        workspace = await self._live(
            session, GenerationWorkspace, run.workspace_id, "generation workspace"
        )
        intent = await self._live(
            session,
            GenerationRunIntent,
            run.intent_id,
            "generation run intent",
        )
        if (run.owner_user_id, run.project_id) != (
            row.owner_user_id,
            row.project_id,
        ) or (workspace.owner_user_id, workspace.project_id) != (
            row.owner_user_id,
            row.project_id,
        ):
            raise MediaOperationsAuthorizationError("Studio output scope mismatch")
        if (
            intent.owner_user_id,
            intent.project_id,
        ) != (run.owner_user_id, run.project_id):
            raise MediaOperationsAuthorizationError("Studio run intent scope mismatch")
        if (
            workspace.external_workspace_id != run.external_workspace_id
            or workspace.external_project_id != run.external_project_id
        ):
            raise MediaOperationsAuthorizationError("Studio workspace/project scope mismatch")
        origin = os.getenv("AOITALK_GENERATION_STUDIO_ASSET_ORIGIN", "").rstrip("/")
        tokens = self.studio_token_provider or generation_studio_token_provider()
        parsed = urlsplit(origin)
        if (
            not origin
            or not tokens.configured
            or origin != workspace.base_url.rstrip("/")
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise MediaAssetError(
                "asset_studio_unconfigured",
                "Studio の認証済み asset origin を server 設定に登録してください",
                503,
            )
        if not re.fullmatch(r"run_[A-Za-z0-9_-]{8,160}", run.external_run_id):
            raise MediaAssetError(
                "asset_studio_reference_invalid", "Studio run ID が無効です"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", str(intent.external_idempotency_key)):
            raise MediaAssetError(
                "asset_studio_reference_invalid", "Studio idempotency key が無効です"
            )
        if not re.fullmatch(r"wsp_[A-Za-z0-9_-]{8,160}", run.external_workspace_id):
            raise MediaAssetError(
                "asset_studio_reference_invalid", "Studio workspace ID が無効です"
            )
        if not re.fullmatch(r"prj_[A-Za-z0-9_-]{8,160}", run.external_project_id):
            raise MediaAssetError(
                "asset_studio_reference_invalid", "Studio project ID が無効です"
            )
        async with self.http_client_factory() as client:
            token = None
            try:
                async for candidate in tokens.attempts(
                    origin, run.external_project_id
                ):
                    async with client.stream(
                        "GET",
                        f"{origin}/api/v1/jobs/by-idempotency/{intent.external_idempotency_key}",
                        follow_redirects=False,
                        headers={
                            "Authorization": f"Bearer {candidate}",
                            "Accept": "application/json",
                        },
                    ) as response:
                        if response.status_code == 401:
                            continue
                        if response.status_code != 200:
                            raise MediaAssetError(
                                "asset_studio_unavailable",
                                "Studio output を取得できません",
                                503,
                            )
                        manifest = bytearray()
                        async for block in response.aiter_bytes(CHUNK_BYTES):
                            manifest.extend(block)
                            if len(manifest) > CHUNK_BYTES:
                                raise MediaAssetError(
                                    "asset_manifest_invalid",
                                    "Studio output manifest が大きすぎます",
                                )
                    token = candidate
                    break
            except GenerationStudioUnavailableError as exc:
                if exc.code != "studio_auth_failed":
                    raise MediaAssetError(
                        "asset_studio_unavailable", "Studio output を取得できません", 503
                    ) from None
            if token is None:
                raise MediaAssetError(
                    "asset_studio_auth_failed", "Studio の認証に失敗しました", 503
                )
            import json

            try:
                body = json.loads(manifest)
            except (ValueError, TypeError):
                raise MediaAssetError(
                    "asset_manifest_invalid", "Studio output manifest が無効です"
                ) from None
            if not isinstance(body, Mapping):
                raise MediaAssetError(
                    "asset_manifest_invalid", "Studio output manifest が無効です"
                )
            expected_key = str(intent.external_idempotency_key)
            if (
                body.get("run_id") != run.external_run_id
                or body.get("job_id") != run.external_run_id
                or body.get("workspace_id") != run.external_workspace_id
                or body.get("project_id") != run.external_project_id
                or body.get("status") != "succeeded"
                or body.get("idempotency_key") != expected_key
                or body.get("external_idempotency_key") != expected_key
            ):
                raise MediaAssetError(
                    "asset_manifest_mismatch",
                    "Studio の receipt scope / intent が一致しません",
                    409,
                )
            output_asset_ids = body.get("output_asset_ids")
            output_asset_urls = body.get("output_asset_urls")
            output_version_ids = body.get("output_version_ids")
            outputs = body.get("outputs")
            if any(
                not isinstance(value, list)
                for value in (
                    output_asset_ids,
                    output_asset_urls,
                    output_version_ids,
                    outputs,
                )
            ):
                raise MediaAssetError(
                    "asset_manifest_invalid", "Studio output receipt が無効です"
                )
            if len(output_asset_ids) != len(output_asset_urls) or len(
                output_asset_ids
            ) != len(output_version_ids):
                raise MediaAssetError(
                    "asset_manifest_mismatch",
                    "Studio output の asset/version/link が一致しません",
                    409,
                )
            if len(output_asset_ids) > 20:
                raise MediaAssetError(
                    "asset_manifest_invalid", "Studio output が多すぎます"
                )
            matching_indices = []
            for index, (asset_id, content_url, version_id) in enumerate(
                zip(output_asset_ids, output_asset_urls, output_version_ids)
            ):
                if (
                    not isinstance(asset_id, str)
                    or not re.fullmatch(r"ast_[A-Za-z0-9_-]{8,160}", asset_id)
                    or not isinstance(version_id, str)
                    or not re.fullmatch(r"outv_[A-Za-z0-9_-]{8,160}", version_id)
                    or not isinstance(content_url, str)
                    or content_url != f"/api/v1/assets/{asset_id}/content"
                ):
                    raise MediaAssetError(
                        "asset_content_url_invalid",
                        "Studio の canonical asset route が無効です",
                    )
                if asset_id == row.external_asset_id:
                    matching_indices.append(index)
            if len(matching_indices) != 1:
                raise MediaAssetError(
                    "asset_manifest_mismatch",
                    "Studio の asset revision が一致しません",
                    409,
                )
            output_index = matching_indices[0]
            if output_version_ids[output_index] != row.external_output_version:
                raise MediaAssetError(
                    "asset_manifest_mismatch",
                    "Studio の output version が一致しません",
                    409,
                )
            matches = [
                value
                for value in outputs
                if isinstance(value, Mapping)
                and value.get("asset_id") == row.external_asset_id
                and value.get("output_version") == row.external_output_version
            ]
            if len(matches) != 1:
                raise MediaAssetError(
                    "asset_manifest_mismatch",
                    "Studio の typed output が一致しません",
                    409,
                )
            output = matches[0]
            if (
                output.get("sha256") != row.sha256
                or output.get("mime_type") != row.mime_type
                or output.get("provenance_hash") != row.provenance_hash
                or (
                    row.width is not None and output.get("width") != row.width
                )
                or (
                    row.height is not None and output.get("height") != row.height
                )
            ):
                raise MediaAssetError(
                    "asset_manifest_mismatch",
                    "Studio の SHA / MIME / output binding が一致しません",
                    409,
                )
            expected_path = f"/api/v1/assets/{row.external_asset_id}/content"
            async with client.stream(
                "GET",
                origin + expected_path,
                follow_redirects=False,
                headers={"Authorization": f"Bearer {token}", "Accept": row.mime_type},
            ) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("content-type", "").split(";", 1)[0].lower()
                    != row.mime_type
                    or response.headers.get("x-content-checksum", "").lower()
                    != row.sha256
                ):
                    raise MediaAssetError(
                        "asset_studio_unavailable",
                        "Studio byte response が無効です",
                        503,
                    )
                content_length = response.headers.get("content-length")
                try:
                    expected_size = int(content_length)
                except (TypeError, ValueError):
                    raise MediaAssetError(
                        "asset_studio_unavailable",
                        "Studio byte response の size が無効です",
                        503,
                    ) from None
                if expected_size <= 0 or expected_size > _limit(row.mime_type):
                    raise MediaAssetError(
                        "asset_studio_unavailable",
                        "Studio byte response の size が無効です",
                        503,
                    )
                received = 0
                async for block in response.aiter_bytes(CHUNK_BYTES):
                    if not isinstance(block, bytes):
                        raise MediaAssetError(
                            "asset_studio_unavailable",
                            "Studio byte response が無効です",
                            503,
                        )
                    received += len(block)
                    if received > expected_size:
                        raise MediaAssetError(
                            "asset_hash_mismatch",
                            "Studio byte response の size が一致しません",
                            409,
                        )
                    yield block
                if received != expected_size:
                    raise MediaAssetError(
                        "asset_hash_mismatch",
                        "Studio byte response の size が一致しません",
                        409,
                    )

    async def upload(
        self,
        session,
        actor,
        *,
        persona_id,
        stream: AsyncIterable[bytes],
        mime_type,
        idempotency_key,
        content_item_id=None,
        expected_content_item_hash=None,
        expected_sha256=None,
        source_kind="upload",
        source_id=None,
        output=None,
    ):
        mime = _mime(mime_type)
        expected_sha256 = _sha(expected_sha256)
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 255
        ):
            raise MediaAssetError("asset_key_invalid", "Idempotency-Key が必要です")
        persona = await self._target(
            session, actor, persona_id, content_item_id, expected_content_item_hash
        )
        target_scope = (persona.owner_user_id, persona.project_id)
        output_id = output.id if output is not None else uuid4()
        root, create = self._stage(persona.owner_user_id, output_id, mime)
        stage, relative, inode = await storage_io.run(
            root.io_key, create, mutation=True
        )
        published = False
        committed = False
        try:
            await self._ingest(root, stage, stream, mime, inode)
            digest, size, _ = await storage_io.run(
                root.io_key,
                lambda: _verify_file(root, stage, mime, expected_sha256),
                timeout=120,
            )
            persona = await self._target(
                session, actor, persona_id, content_item_id, expected_content_item_hash
            )
            if (persona.owner_user_id, persona.project_id) != target_scope:
                raise MediaAssetError(
                    "asset_scope_changed",
                    "upload 中に persona scope が変更されました",
                    409,
                )
            command = _asset_command_hash(
                persona_id=persona.id,
                content_item_id=content_item_id,
                expected_content_item_hash=expected_content_item_hash,
                source_kind=source_kind,
                source_id=source_id,
                sha256=digest,
                mime_type=mime,
                size_bytes=size,
            )
            prior = (
                await session.execute(
                    select(MediaAssetRevision)
                    .where(
                        MediaAssetRevision.persona_id == persona.id,
                        MediaAssetRevision.idempotency_key == idempotency_key,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if prior:
                if prior.command_hash != command:
                    raise MediaOperationsConflictError(
                        "Idempotency-Key の metadata / bytes が変更されています"
                    )
                await self._verified(
                    session,
                    prior.generation_output_id,
                    persona.owner_user_id,
                    persona.project_id,
                    digest,
                )
                return _receipt(prior)
            if output is not None:
                existing = (
                    await session.execute(
                        select(MediaAssetRevision).where(
                            MediaAssetRevision.generation_output_id == output.id
                        )
                    )
                ).scalar_one_or_none()
                if existing:
                    raise MediaOperationsConflictError(
                        "GenerationOutput は既に登録されています"
                    )
                # Re-read the live output after network/file copy, including deletion.
                live = await self._live(session, GenerationOutput, output.id)
                if live.byte_binding_state != "external":
                    raise MediaAssetError(
                        "asset_binding_missing",
                        "登録済み素材の byte binding が失われています",
                        409,
                    )
                if (
                    live.sha256,
                    live.mime_type,
                    live.owner_user_id,
                    live.project_id,
                ) != (digest, mime, persona.owner_user_id, persona.project_id):
                    raise MediaAssetError(
                        "asset_changed", "GenerationOutput が変更されました", 409
                    )
                live.byte_binding_state = "managed"
            else:
                session.add(
                    GenerationOutput(
                        id=output_id,
                        generation_run_id=None,
                        byte_binding_state="managed",
                        owner_user_id=persona.owner_user_id,
                        project_id=persona.project_id,
                        external_asset_id=f"ast_{output_id.hex}",
                        external_output_version=f"outv_{output_id.hex}",
                        sha256=digest,
                        mime_type=mime,
                        deep_link=None,
                        provenance_hash=command,
                    )
                )
                await session.flush()
            revision = MediaAssetRevision(
                id=uuid4(),
                generation_output_id=output_id,
                owner_user_id=persona.owner_user_id,
                project_id=persona.project_id,
                persona_id=persona.id,
                content_item_id=UUID(str(content_item_id)) if content_item_id else None,
                source_kind=source_kind,
                source_id=UUID(str(source_id)) if source_id else None,
                relative_path="generated_media/" + relative,
                sha256=digest,
                mime_type=mime,
                size_bytes=size,
                revision_hash="",
                command_hash=command,
                idempotency_key=idempotency_key,
            )
            revision.revision_hash = sha256_json(_binding(revision))
            session.add(revision)
            await session.flush()

            def publish():
                check_io_cancelled()
                publish_asset(root, stage, relative, inode)

            await storage_io.run(root.io_key, publish, mutation=True)
            published = True
            await storage_io.run(
                root.io_key,
                lambda: _verify_file(root, relative, mime, digest, size),
                timeout=120,
            )
            receipt = _receipt(revision)
            await session.commit()
            committed = True
            return receipt
        except IntegrityError as exc:
            await session.rollback()
            raise MediaOperationsConflictError(
                "素材登録が競合しました。登録状態を確認してください"
            ) from exc
        except BaseException:
            await session.rollback()
            raise
        finally:
            await self._remove(root, stage, inode)
            if published and not committed:
                await self._remove(root, relative, inode)

    async def register(
        self,
        session,
        actor,
        *,
        persona_id,
        source_kind,
        source_id,
        idempotency_key,
        content_item_id=None,
        expected_content_item_hash=None,
        expected_sha256=None,
    ):
        persona = await self._target(
            session, actor, persona_id, content_item_id, expected_content_item_hash
        )
        output = None
        if source_kind == "generated_media":
            media = await self._live(session, GeneratedMedia, source_id)
            if (
                media.status != "succeeded"
                or str(media.owner_user_id) != str(persona.owner_user_id)
                or str(_actor_id(actor)) != str(media.owner_user_id)
            ):
                raise MediaOperationsAuthorizationError(
                    "GeneratedMedia owner / 状態が一致しません"
                )
            root, relative, mime, sha, size = (
                self._root(),
                media_relative_path(media.relative_path),
                _mime(media.mime_type),
                expected_sha256,
                media.byte_size,
            )
            stream = self._source_stream(root, relative, mime, sha, size)
        elif source_kind == "library_file":
            root, relative, mime, sha, size = await self._library(
                session, actor, source_id, persona
            )
            if (
                expected_sha256 is not None
                and sha is not None
                and _sha(expected_sha256) != sha
            ):
                raise MediaAssetError(
                    "asset_hash_mismatch", "Library の hash が一致しません", 409
                )

            async def library_stream():
                async for block in self._source_stream(
                    root, relative, mime, sha or expected_sha256, size
                ):
                    yield block
                # Source grants, root enrollment and the logical ID must still
                # be valid after a long copy; a revoked Files grant cannot be
                # carried into the final registration transaction.
                fresh_persona = await self._live(
                    session, Persona, persona.id, "persona"
                )
                fresh = await self._library(session, actor, source_id, fresh_persona)
                if (fresh[0], fresh[1], fresh[2], fresh[4]) != (
                    root,
                    relative,
                    mime,
                    size,
                ):
                    raise MediaAssetError(
                        "asset_source_changed",
                        "Library 素材の参照が変更されました",
                        409,
                    )

            stream = library_stream()
        elif source_kind == "generation_output":
            output = await self._live(session, GenerationOutput, source_id)
            await self._assert_entity_access(session, actor, output, permission="read")
            if (output.owner_user_id, output.project_id) != (
                persona.owner_user_id,
                persona.project_id,
            ):
                raise MediaOperationsAuthorizationError(
                    "GenerationOutput scope mismatch"
                )
            if expected_sha256 is not None and _sha(expected_sha256) != output.sha256:
                raise MediaAssetError(
                    "asset_hash_mismatch", "GenerationOutput hash が一致しません", 409
                )
            existing_revision = await self._revision(session, output.id)
            if existing_revision is not None:
                # A completed import is already an owned, immutable asset.  Do
                # not contact Studio again on an idempotent retry; verify the
                # local bytes and ledger instead.  A different command may not
                # silently reuse the same GenerationOutput because the ledger
                # binds one output to one persona/content target.
                if output.byte_binding_state != "managed":
                    raise MediaAssetError(
                        "asset_binding_invalid",
                        "素材の登録状態が一致しません",
                        409,
                    )
                verified = await self._verified(
                    session,
                    output.id,
                    persona.owner_user_id,
                    persona.project_id,
                    output.sha256,
                )
                if verified is None:
                    raise MediaAssetError(
                        "asset_binding_missing",
                        "登録済み素材の byte binding が失われています",
                        409,
                    )
                prior_key = (
                    await session.execute(
                        select(MediaAssetRevision)
                        .where(
                            MediaAssetRevision.persona_id == persona.id,
                            MediaAssetRevision.idempotency_key == idempotency_key,
                        )
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if (
                    prior_key is not None
                    and prior_key.generation_output_id != output.id
                ):
                    raise MediaOperationsConflictError(
                        "Idempotency-Key は別の素材登録に使用されています"
                    )
                current_command = _asset_command_hash(
                    persona_id=persona.id,
                    content_item_id=content_item_id,
                    expected_content_item_hash=expected_content_item_hash,
                    source_kind="generation_output",
                    source_id=output.id,
                    sha256=output.sha256,
                    mime_type=_mime(output.mime_type),
                    size_bytes=verified[0].size_bytes,
                )
                if (
                    existing_revision.persona_id != persona.id
                    or existing_revision.source_kind != "generation_output"
                    or existing_revision.source_id != output.id
                    or existing_revision.command_hash != current_command
                ):
                    raise MediaOperationsConflictError(
                        "GenerationOutput は既に登録されています"
                    )
                return _receipt(verified[0])
            if output.byte_binding_state == "managed":
                raise MediaAssetError(
                    "asset_binding_missing",
                    "登録済み素材の byte binding が失われています",
                    409,
                )
            # Reject reuse of an idempotency key before opening a Studio
            # stream.  This keeps retries deterministic even when the prior
            # command targeted another source and avoids an unnecessary
            # remote read before reporting the conflict.
            prior_key = (
                await session.execute(
                    select(MediaAssetRevision)
                    .where(
                        MediaAssetRevision.persona_id == persona.id,
                        MediaAssetRevision.idempotency_key == idempotency_key,
                    )
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if prior_key is not None:
                raise MediaOperationsConflictError(
                    "Idempotency-Key は別の素材登録に使用されています"
                )
            mime, expected_sha256 = _mime(output.mime_type), output.sha256
            stream = self._studio_stream(session, output)
        else:
            raise MediaAssetError("asset_source_invalid", "source_kind が無効です")
        return await self.upload(
            session,
            actor,
            persona_id=persona.id,
            stream=stream,
            mime_type=mime,
            idempotency_key=idempotency_key,
            content_item_id=content_item_id,
            expected_content_item_hash=expected_content_item_hash,
            expected_sha256=expected_sha256,
            source_kind=source_kind,
            source_id=source_id,
            output=output,
        )

    async def ingest_generation_output(
        self,
        session,
        actor,
        *,
        persona_id,
        generation_output_id,
        content_item_id=None,
        expected_content_item_hash=None,
        expected_sha256=None,
        idempotency_key=None,
    ):
        """Import one reconciled Studio output into owned media storage.

        This is intentionally a material-import command only.  It does not
        select an output, adopt it into content, or publish it.  The default
        key is derived from the immutable output identity so a worker can call
        this after every successful reconciliation without issuing another
        Studio request once the bytes are already managed locally.
        """
        try:
            output_id = UUID(str(generation_output_id))
        except (ValueError, TypeError, AttributeError):
            raise MediaAssetError("asset_id_invalid", "素材 ID が無効です") from None
        if idempotency_key is None:
            idempotency_key = f"generation-output:{output_id}"
        return await self.register(
            session,
            actor,
            persona_id=persona_id,
            source_kind="generation_output",
            source_id=output_id,
            idempotency_key=idempotency_key,
            content_item_id=content_item_id,
            expected_content_item_hash=expected_content_item_hash,
            expected_sha256=expected_sha256,
        )

    async def _revision(self, session, output_id):
        try:
            output_id = UUID(str(output_id))
        except (ValueError, TypeError, AttributeError):
            raise MediaAssetError("asset_id_invalid", "素材 ID が無効です") from None
        return (
            await session.execute(
                select(MediaAssetRevision)
                .where(MediaAssetRevision.generation_output_id == output_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def _verified(self, session, output_id, owner, project, expected):
        try:
            output_id = UUID(str(output_id))
        except (ValueError, TypeError, AttributeError):
            raise MediaAssetError("asset_id_invalid", "素材 ID が無効です") from None
        # Classify output + ledger in one statement snapshot. Separate SELECTs
        # could combine an old external marker with a newly deleted binding.
        row = (
            await session.execute(
                select(GenerationOutput, MediaAssetRevision)
                .outerjoin(
                    MediaAssetRevision,
                    MediaAssetRevision.generation_output_id == GenerationOutput.id,
                )
                .where(GenerationOutput.id == output_id)
                .execution_options(populate_existing=True)
            )
        ).first()
        if row is None:
            raise MediaOperationsNotFoundError("GenerationOutput not found")
        output, revision = row
        if (output.owner_user_id, output.project_id) != (
            UUID(str(owner)),
            UUID(str(project)) if project else None,
        ):
            raise MediaOperationsAuthorizationError(
                "素材の owner/project scope が一致しません"
            )
        if output.sha256 != _sha(expected):
            raise MediaAssetError(
                "asset_revision_mismatch", "素材の SHA256 が一致しません", 409
            )
        if revision is None:
            if (
                output.byte_binding_state != "external"
                or output.generation_run_id is None
            ):
                raise MediaAssetError(
                    "asset_binding_missing",
                    "登録済み素材の byte binding が失われています",
                    409,
                )
            return None
        if output.byte_binding_state != "managed":
            raise MediaAssetError(
                "asset_binding_invalid", "素材の登録状態が一致しません", 409
            )
        if (revision.owner_user_id, revision.project_id) != (
            UUID(str(owner)),
            UUID(str(project)) if project else None,
        ) or (output.owner_user_id, output.project_id) != (
            revision.owner_user_id,
            revision.project_id,
        ):
            raise MediaOperationsAuthorizationError(
                "素材の owner/project scope が一致しません"
            )
        if (
            revision.sha256 != _sha(expected)
            or output.sha256 != revision.sha256
            or output.mime_type != revision.mime_type
            or revision.revision_hash != sha256_json(_binding(revision))
        ):
            raise MediaAssetError(
                "asset_revision_mismatch",
                "素材の immutable revision が一致しません",
                409,
            )
        persona = await self._live(session, Persona, revision.persona_id, "persona")
        if (persona.owner_user_id, persona.project_id) != (
            revision.owner_user_id,
            revision.project_id,
        ):
            raise MediaOperationsAuthorizationError(
                "素材 persona の scope が一致しません"
            )
        root = self._root()
        relative = media_relative_path(revision.relative_path)
        if (
            relative
            != f"operations/{revision.owner_user_id}/{revision.generation_output_id}{MIME_EXTENSIONS[_mime(revision.mime_type)]}"
        ):
            raise MediaAssetError("asset_path_invalid", "素材の管理参照が無効です", 403)
        _, _, identity = await storage_io.run(
            root.io_key,
            lambda: _verify_file(
                root, relative, revision.mime_type, revision.sha256, revision.size_bytes
            ),
            timeout=120,
        )
        return revision, root, relative, identity

    async def get_asset(self, session, actor, output_id):
        revision = await self._revision(session, output_id)
        if revision is None:
            raise MediaOperationsNotFoundError("asset not found")
        await self._assert_entity_access(session, actor, revision, permission="read")
        return _receipt(revision)

    async def stream_preview(self, root, relative, identity, start, end):
        offset = start
        while offset <= end:

            def read():
                with open_asset(root, relative) as (handle, info):
                    if _identity(info) != identity:
                        raise MediaAssetError(
                            "asset_changed", "配信中に素材が変更されました", 409
                        )
                    handle.seek(offset)
                    block = handle.read(min(CHUNK_BYTES, end - offset + 1))
                    if (
                        _identity(os.fstat(handle.fileno())) != identity
                        or _identity(root.resolve(relative).lstat()) != identity
                    ):
                        raise MediaAssetError(
                            "asset_changed", "配信中に素材が変更されました", 409
                        )
                    check_io_cancelled()
                    return block

            block = await storage_io.run(root.io_key, read)
            if not block:
                raise MediaAssetError(
                    "asset_unavailable", "素材の配信が中断されました", 409
                )
            offset += len(block)
            yield block

    async def prepare_preview(self, session, actor, output_id, expected_sha256=None):
        revision = await self._revision(session, output_id)
        if revision is None:
            raise MediaOperationsNotFoundError("asset not found")
        await self._assert_entity_access(session, actor, revision, permission="read")
        return await self._verified(
            session,
            output_id,
            revision.owner_user_id,
            revision.project_id,
            expected_sha256 or revision.sha256,
        )

    async def materialize_for_provider(
        self,
        session,
        generation_output_id,
        *,
        owner_user_id,
        project_id,
        expected_sha256,
        target,
    ):
        prepared = await self._verified(
            session, generation_output_id, owner_user_id, project_id, expected_sha256
        )
        if prepared is None:
            return None
        revision, root, relative, _ = prepared
        snapshot = (
            revision.revision_hash,
            revision.mime_type,
            revision.size_bytes,
            revision.sha256,
        )
        target = lexical_path(target)
        target_root = StorageRoot(
            id="provider-scratch",
            name="Provider scratch",
            root_path=str(target.parent),
            external=False,
        )

        def create_target():
            with open_asset(
                target_root, target.name, flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL
            ) as (_, info):
                return info.st_dev, info.st_ino

        inode = await storage_io.run(target_root.io_key, create_target, mutation=True)
        digest, total = hashlib.sha256(), 0
        try:
            async for block in self._source_stream(
                root, relative, snapshot[1], snapshot[3], snapshot[2]
            ):
                digest.update(block)
                total += len(block)

                def write():
                    with open_asset(
                        target_root,
                        target.name,
                        flags=os.O_WRONLY | os.O_APPEND,
                        expected_inode=inode,
                    ) as (handle, _):
                        check_io_cancelled()
                        handle.write(block)

                await storage_io.run(target_root.io_key, write, mutation=True)
            await storage_io.run(
                target_root.io_key,
                lambda: _verify_file(
                    target_root, target.name, snapshot[1], snapshot[3], snapshot[2]
                ),
                timeout=120,
            )
            # Fresh ledger read after copying closes deletion/change races.
            fresh = await self._verified(
                session,
                generation_output_id,
                owner_user_id,
                project_id,
                expected_sha256,
            )
            if (
                fresh is None
                or (
                    fresh[0].revision_hash,
                    fresh[0].mime_type,
                    fresh[0].size_bytes,
                    fresh[0].sha256,
                )
                != snapshot
                or total != snapshot[2]
                or not hmac.compare_digest(digest.hexdigest(), expected_sha256)
            ):
                raise MediaAssetError(
                    "asset_changed", "provider 送信前に素材が変更されました", 409
                )
            return VerifiedMediaAsset(
                UUID(str(generation_output_id)),
                target,
                digest.hexdigest(),
                snapshot[1],
                total,
            )
        except BaseException:
            await self._remove(target_root, target.name, inode)
            raise

    async def list_assets(self, session, actor, persona_id, limit=100):
        persona = await self._live(session, Persona, persona_id, "persona")
        await self._assert_entity_access(session, actor, persona, permission="read")
        rows = (
            (
                await session.execute(
                    select(MediaAssetRevision)
                    .where(MediaAssetRevision.persona_id == persona.id)
                    .order_by(
                        MediaAssetRevision.created_at.desc(),
                        MediaAssetRevision.id.desc(),
                    )
                    .limit(min(100, max(1, limit)))
                )
            )
            .scalars()
            .all()
        )
        return [_receipt(row) for row in rows]

    async def list_sources(
        self, session, actor, persona_id, limit=100, *, unavailable_roots=None
    ):
        persona = await self._live(session, Persona, persona_id, "persona")
        await self._assert_entity_access(session, actor, persona, permission="read")
        limit = min(100, max(1, limit))
        entries = []
        if _actor_id(actor) == persona.owner_user_id:
            media = (
                (
                    await session.execute(
                        select(GeneratedMedia)
                        .where(
                            GeneratedMedia.owner_user_id == str(persona.owner_user_id),
                            GeneratedMedia.status == "succeeded",
                            GeneratedMedia.mime_type.in_(MIME_EXTENSIONS),
                        )
                        .order_by(GeneratedMedia.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            entries += [
                {
                    "source_kind": "generated_media",
                    "source_id": row.id,
                    "mime_type": row.mime_type,
                    "size_bytes": row.byte_size,
                    "sha256": None,
                }
                for row in media
            ]
        outputs = (
            (
                await session.execute(
                    select(GenerationOutput)
                    .where(
                        GenerationOutput.owner_user_id == persona.owner_user_id,
                        GenerationOutput.project_id == persona.project_id,
                        GenerationOutput.mime_type.in_(MIME_EXTENSIONS),
                        GenerationOutput.generation_run_id.is_not(None),
                    )
                    .order_by(GenerationOutput.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        entries += [
            {
                "source_kind": "generation_output",
                "source_id": row.id,
                "mime_type": row.mime_type,
                "size_bytes": None,
                "sha256": row.sha256,
            }
            for row in outputs
        ]
        warnings = unavailable_roots if unavailable_roots is not None else set()
        for ident, _, _, mime, size in await self._library_entries(
            session, actor, persona, limit=limit, unavailable_roots=warnings
        ):
            entries.append(
                {
                    "source_kind": "library_file",
                    "source_id": ident,
                    "mime_type": mime,
                    "size_bytes": size,
                    "sha256": None,
                }
            )
        # Each source kind must remain selectable when another has a full
        # page. Merge the bounded catalogs fairly instead of truncating the
        # appended Library catalog behind 100 generated outputs.
        groups = [
            [entry for entry in entries if entry["source_kind"] == kind]
            for kind in ("generated_media", "generation_output", "library_file")
        ]
        return [
            entry for row in zip_longest(*groups) for entry in row if entry is not None
        ][:limit]
