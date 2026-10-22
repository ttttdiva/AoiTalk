"""Files selection -> verified immutable Artifact -> pinned manual download."""

from __future__ import annotations

from pathlib import PurePosixPath
from uuid import UUID

from ..memory.models import ArtifactVersion, Persona
from .artifact_snapshot_storage import (
    MAX_SNAPSHOT_BYTES,
    copy_snapshot,
    prepare_snapshot,
    product_file_limit,
    source_identity,
    stream_snapshot,
)
from .media_asset_service import MediaAssetService
from .media_asset_service import _identity
from .media_operations_package_service import (
    _artifact_ref,
    _filename,
    _row,
    _same_scope,
    _snapshot,
)
from .media_operations_service import _actor_id
from .operations_service import (
    OperationsService,
    OperationsAuthorizationError,
    OperationsConflictError,
    sha256_json,
    canonical_json,
)
from .storage_files import _etag
from .media_asset_storage import open_asset
from .storage_io import StorageError, storage_io


class MediaProductFileService:
    def __init__(self, assets=None, operations=None):
        self.assets = assets or MediaAssetService()
        self.operations = operations or OperationsService()

    async def _persona(self, session, actor, persona_id, *, write=False):
        persona = await self.assets._live(
            session, Persona, UUID(str(persona_id)), "persona"
        )
        await self.assets._assert_entity_access(
            session, actor, persona, permission="write" if write else "read"
        )
        if write and persona.owner_user_id != _actor_id(actor):
            raise OperationsAuthorizationError(
                "product Artifact import requires its owner"
            )
        return persona

    async def _entries(
        self, session, actor, persona, *, source_id=None, limit=100, file_name=""
    ):
        maximum = product_file_limit()
        return await self.assets._library_entries(
            session,
            actor,
            persona,
            limit=limit,
            source_id=source_id,
            file_filter=lambda entry: (
                type(entry["size_bytes"]) is int
                and 0 < entry["size_bytes"] <= maximum
                and file_name.casefold() in entry["name"].casefold()
            ),
        )

    async def _selected(self, session, actor, persona, source_id):
        entries = await self._entries(
            session, actor, persona, source_id=source_id, limit=1
        )
        if not entries:
            raise StorageError(
                "file_not_found",
                "選択したFilesの配布物を取得できません。権限とサイズ上限を確認してください",
                404,
            )
        return entries[0]

    async def _describe(self, entry, persona):
        ident, root, relative, mime, _size = entry

        def inspect():
            with open_asset(root, relative) as (_, info):
                identity = source_identity(root, relative)
                if _identity(info) != identity:
                    raise OperationsConflictError(
                        "Files selection changed during inspection"
                    )
                return _etag(info), info.st_size, identity

        etag, size, identity = await storage_io.run(root.io_key, inspect)
        return {
            "source_id": str(ident),
            "filename": PurePosixPath(relative).name,
            "owner_user_id": str(persona.owner_user_id),
            "project_id": str(persona.project_id) if persona.project_id else None,
            "mime_type": mime,
            "size_bytes": size,
            "etag": etag,
        }, identity

    async def list_files(self, session, actor, persona_id, limit=100, file_name=""):
        persona = await self._persona(session, actor, persona_id)
        rows = []
        for entry in await self._entries(
            session, actor, persona, limit=limit, file_name=file_name
        ):
            row, _ = await self._describe(entry, persona)
            if 0 < row["size_bytes"] <= product_file_limit():
                rows.append(row)
        return {"max_bytes": product_file_limit(), "files": rows}

    async def import_file(
        self,
        session,
        actor,
        *,
        persona_id,
        source_id,
        expected_etag,
        expected_size_bytes,
        disconnected=None,
    ):
        try:
            persona = await self._persona(session, actor, persona_id, write=True)
            selected = await self._selected(session, actor, persona, source_id)
            row, identity = await self._describe(selected, persona)
            if row["etag"] != expected_etag or row["size_bytes"] != expected_size_bytes:
                raise OperationsConflictError(
                    "Files selection changed; reload and select it again"
                )
            filename = _filename(row["filename"], str(source_id))
            _, root, relative, mime, _ = selected
            owner, project = persona.owner_user_id, persona.project_id

            async def validate_source():
                fresh = await self._persona(session, actor, persona_id, write=True)
                entry = await self._selected(session, actor, fresh, source_id)
                fresh_row, fresh_identity = await self._describe(entry, fresh)
                if (
                    (fresh.owner_user_id, fresh.project_id) != (owner, project)
                    or entry[1:4] != selected[1:4]
                    or fresh_identity != identity
                    or fresh_row["etag"] != expected_etag
                ):
                    raise OperationsConflictError(
                        "Files source or permission changed during import"
                    )

            snapshot = await copy_snapshot(
                root,
                relative,
                identity,
                validate_source=validate_source,
                disconnected=disconnected,
            )
            artifact = await self.operations.create_artifact(
                session,
                actor,
                filename=filename,
                mime_type=mime,
                project_id=project,
                provenance={
                    "source_kind": "files_snapshot",
                    "files_source_id": str(source_id),
                    "source_etag": expected_etag,
                    "persona_id": str(persona_id),
                },
                _snapshot=snapshot,
                _defer_commit=True,
            )
            # Every reuse is still an explicit returned version ID. The existing
            # owner/project/content identity index only deduplicates registration.
            saved = await _row(
                self.operations, session, ArtifactVersion, artifact["id"]
            )
            if (
                saved.owner_user_id,
                saved.project_id,
                saved.sha256,
                saved.size_bytes,
                saved.mime_type,
            ) != (owner, project, snapshot.sha256, snapshot.size_bytes, mime):
                raise OperationsConflictError(
                    "registered product Artifact scope mismatch"
                )
            await validate_source()
            if disconnected is not None and await disconnected():
                raise StorageError(
                    "product_import_cancelled", "配布物の取込を中止しました", 409
                )
            await session.commit()
            return {
                "artifact_version_id": str(saved.id),
                "owner_user_id": str(owner),
                "project_id": str(project) if project else None,
                "sha256": snapshot.sha256,
                "size_bytes": snapshot.size_bytes,
                "mime_type": mime,
                "filename": saved.filename,
            }
        except BaseException:
            await self.operations._rollback(session)
            raise

    async def artifact_snapshot(self, session, actor, artifact_id):
        artifact = await _row(self.operations, session, ArtifactVersion, artifact_id)
        await self.operations._assert_entity_access(
            session, actor, artifact, permission="read"
        )
        return artifact, await prepare_snapshot(artifact)

    async def validate_artifact(self, session, actor, artifact_id):
        artifact, snapshot = await self.artifact_snapshot(session, actor, artifact_id)
        async for _ in stream_snapshot(snapshot, 0, snapshot.size_bytes - 1):
            pass
        return artifact

    async def deliverable(
        self, session, actor, action_id, expected_action_version, expected_payload_hash
    ):
        action, source, typed, _ = await _snapshot(
            session,
            actor,
            action_id,
            expected_action_version,
            expected_payload_hash,
            self.operations,
        )
        if action.action_type != "media.release_product" or action.platform not in {
            "dlsite",
            "fanza",
        }:
            raise OperationsConflictError("deliverable requires a product Action")
        ident = _artifact_ref(typed.get("deliverable_package_ref"))
        if ident is None:
            raise OperationsConflictError(
                "deliverable requires an explicit ArtifactVersion ID"
            )
        artifact, snapshot = await self.artifact_snapshot(session, actor, ident)
        _same_scope(action, artifact)
        if artifact.sha256 not in action.artifact_hashes:
            raise OperationsConflictError(
                "deliverable hash is not pinned to this Action"
            )
        manifest = {
            "schema_version": "product-deliverable/v1",
            "action": {
                "id": str(action.id),
                "action_version": expected_action_version,
                "payload_hash": expected_payload_hash,
                "platform": action.platform,
            },
            "source": source,
            "deliverable": {
                "artifact_version_id": str(artifact.id),
                "owner_user_id": str(artifact.owner_user_id),
                "project_id": str(artifact.project_id) if artifact.project_id else None,
                "filename": _filename(artifact.filename, str(artifact.id)),
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.mime_type,
            },
            "transport": "verified_chunk_stream",
            "max_bytes": MAX_SNAPSHOT_BYTES,
        }
        return {
            "manifest": manifest,
            "manifest_json": canonical_json(manifest),
            "manifest_sha256": sha256_json(manifest),
        }, snapshot
