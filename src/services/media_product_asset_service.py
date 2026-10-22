"""Explicit, bounded exports from the managed asset ledger to ArtifactVersion.

Only IDs join the ledgers. Exporting bytes creates no submission or receipt.
Large media continues to use the authenticated asset Range/stream endpoint.
"""

from uuid import UUID

from sqlalchemy import select

from ..memory.models.media_assets import MediaAssetRevision
from ..memory.models import ArtifactVersion
from ..memory.models.media_operations_content import ContentVariantRevision
from .media_operations_content_service import _normalize_payload
from .media_asset_service import MediaAssetService, MIME_EXTENSIONS
from .media_operations_service import (
    MediaOperationsAuthorizationError,
    MediaOperationsConflictError,
    MediaOperationsValidationError,
    _actor_id,
    _validated_sha256,
    sha256_json,
)
from .operations_service import OperationsService

MAX_PRODUCT_ARTIFACT_BYTES = 25 * 1024 * 1024


def product_artifact_binding_hash(revision, artifact_id):
    return sha256_json(
        {
            "generation_output_id": str(revision.generation_output_id),
            "asset_revision_id": str(revision.id),
            "revision_hash": revision.revision_hash,
            "owner_user_id": str(revision.owner_user_id),
            "project_id": str(revision.project_id) if revision.project_id else None,
            "artifact_version_id": str(artifact_id),
            "sha256": revision.sha256,
            "mime_type": revision.mime_type,
            "size_bytes": revision.size_bytes,
        }
    )


class MediaProductAssetService:
    def __init__(self, assets=None, operations=None):
        self.assets = assets or MediaAssetService()
        self.operations = operations or OperationsService()

    async def bind(
        self, session, actor, output_id, *, expected_sha256, expected_revision_hash
    ):
        try:
            return await self._bind(
                session,
                actor,
                output_id,
                expected_sha256=expected_sha256,
                expected_revision_hash=expected_revision_hash,
            )
        except BaseException:
            # A failed verifier or cancellation must not leave a caller able to
            # commit an unbound ArtifactVersion or its creation audit event.
            await self.operations._rollback(session)
            raise

    async def _bind(
        self, session, actor, output_id, *, expected_sha256, expected_revision_hash
    ):
        digest = _validated_sha256(expected_sha256, "expected_sha256")
        revision_hash = _validated_sha256(
            expected_revision_hash, "expected_revision_hash"
        )
        revision = await self.assets._revision(session, output_id)
        if revision is None:
            from .media_operations_service import MediaOperationsNotFoundError

            raise MediaOperationsNotFoundError("managed product asset not found")
        await self.assets._assert_entity_access(
            session, actor, revision, permission="write"
        )
        if revision.owner_user_id != _actor_id(actor):
            raise MediaOperationsAuthorizationError(
                "product export requires the asset owner"
            )
        if revision.sha256 != digest or revision.revision_hash != revision_hash:
            raise MediaOperationsConflictError("product asset revision changed")
        if revision.size_bytes > MAX_PRODUCT_ARTIFACT_BYTES:
            raise MediaOperationsValidationError(
                "product ArtifactVersion exceeds 25 MiB; download this asset through its streaming preview"
            )
        snapshot = (
            revision.id,
            revision.owner_user_id,
            revision.project_id,
            revision.sha256,
            revision.revision_hash,
            revision.mime_type,
            revision.size_bytes,
        )
        prepared = await self.assets.prepare_preview(session, actor, output_id, digest)
        _, root, relative, identity = prepared
        if revision.artifact_version_id is not None:
            if revision.artifact_binding_hash != product_artifact_binding_hash(
                revision, revision.artifact_version_id
            ):
                raise MediaOperationsConflictError("product artifact binding changed")
            artifact, data = await self.operations.get_artifact_content(
                session, actor, revision.artifact_version_id
            )
        else:
            data = bytearray()
            async for block in self.assets.stream_preview(
                root, relative, identity, 0, revision.size_bytes - 1
            ):
                data.extend(block)
                if (
                    len(data) > revision.size_bytes
                    or len(data) > MAX_PRODUCT_ARTIFACT_BYTES
                ):
                    raise MediaOperationsConflictError(
                        "product asset grew during export"
                    )
            artifact = await self.operations.create_artifact(
                session,
                actor,
                content=bytes(data),
                project_id=revision.project_id,
                filename=f"{output_id}{MIME_EXTENSIONS[revision.mime_type]}",
                mime_type=revision.mime_type,
                sha256=digest,
                size_bytes=revision.size_bytes,
                provenance={
                    "generation_output_id": str(output_id),
                    "media_asset_revision_id": str(revision.id),
                    "revision_hash": revision_hash,
                },
                _defer_commit=True,
            )
        live = (
            await session.execute(
                select(MediaAssetRevision)
                .where(MediaAssetRevision.id == snapshot[0])
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            live is None
            or (
                live.id,
                live.owner_user_id,
                live.project_id,
                live.sha256,
                live.revision_hash,
                live.mime_type,
                live.size_bytes,
            )
            != snapshot
        ):
            raise MediaOperationsConflictError("product asset changed during export")
        if (
            artifact["owner_user_id"] != str(live.owner_user_id)
            or artifact["project_id"]
            != (str(live.project_id) if live.project_id else None)
            or artifact["sha256"] != digest
            or artifact["mime_type"] != live.mime_type
            or artifact["size_bytes"] != live.size_bytes
            or len(data) != live.size_bytes
        ):
            raise MediaOperationsConflictError(
                "product artifact binding does not match the managed asset"
            )
        artifact_id = UUID(artifact["id"])
        if live.artifact_version_id not in (None, artifact_id):
            raise MediaOperationsConflictError("product artifact binding changed")
        if (
            live.artifact_version_id is not None
            and live.artifact_binding_hash
            != product_artifact_binding_hash(live, artifact_id)
        ):
            raise MediaOperationsConflictError(
                "product artifact binding changed during export"
            )
        # Verify live output + bytes before the one artifact/binding DB commit.
        await self.assets.prepare_preview(session, actor, output_id, digest)
        live.artifact_version_id = artifact_id
        live.artifact_binding_hash = product_artifact_binding_hash(live, artifact_id)
        await session.commit()
        return {
            "generation_output_id": str(output_id),
            "asset_revision_id": str(live.id),
            "revision_hash": revision_hash,
            "artifact_version_id": str(artifact_id),
            "owner_user_id": str(live.owner_user_id),
            "project_id": str(live.project_id) if live.project_id else None,
            "sha256": digest,
            "mime_type": live.mime_type,
            "size_bytes": live.size_bytes,
        }

    async def prepare_action(self, session, actor, **command):
        """Reuse the manual Action proposal boundary with server-verified pins."""
        if (
            command.get("platform") not in {"dlsite", "fanza"}
            or command.get("action_type") != "media.release_product"
            or command.get("execution_mode", "manual") != "manual"
        ):
            raise MediaOperationsValidationError(
                "product handoff requires a manual release_product proposal"
            )
        revision = await self.operations._get_or_404(
            session,
            ContentVariantRevision,
            command.get("content_variant_revision_id"),
            "content_variant_revision_id",
        )
        await self.operations._assert_entity_access(
            session, actor, revision, permission="write"
        )
        if revision.platform != command["platform"]:
            raise MediaOperationsConflictError(
                "product platform does not match saved revision"
            )
        typed, refs = _normalize_payload(revision.payload_json, revision.platform)
        from .media_operations_package_service import _artifact_ref

        deliverable_id = _artifact_ref(typed["deliverable_package_ref"])
        if deliverable_id is None:
            raise MediaOperationsValidationError(
                "deliverable requires an explicit artifact:ID reference"
            )
        artifact = await self.operations._get_or_404(
            session, ArtifactVersion, deliverable_id, "artifact_id"
        )
        await self.operations._assert_entity_access(
            session, actor, artifact, permission="read"
        )
        if (artifact.owner_user_id, artifact.project_id) != (
            revision.owner_user_id,
            revision.project_id,
        ):
            raise MediaOperationsAuthorizationError(
                "deliverable owner/project scope mismatch"
            )
        from .media_product_file_service import MediaProductFileService

        await MediaProductFileService(
            assets=self.assets, operations=self.operations
        ).validate_artifact(session, actor, deliverable_id)
        hashes = [artifact.sha256]
        pins = {
            ref["generation_output_id"]: ref["sha256"]
            for ref in revision.generation_output_refs_json
        }
        for output_id in refs:
            asset = await self.assets.get_asset(session, actor, output_id)
            if (asset["owner_user_id"], asset["project_id"], asset["sha256"]) != (
                str(revision.owner_user_id),
                str(revision.project_id) if revision.project_id else None,
                pins.get(output_id),
            ):
                raise MediaOperationsConflictError(
                    "product preview does not match the saved output pin"
                )
            binding = await self.bind(
                session,
                actor,
                output_id,
                expected_sha256=pins[output_id],
                expected_revision_hash=asset["revision_hash"],
            )
            hashes.append(binding["sha256"])
        # The existing boundary checks readiness, latest immutable revision,
        # exact typed payload, account/connection scope, and replay conflicts.
        command["artifact_hashes"] = list(dict.fromkeys(hashes))
        return await self.operations.create_media_action(session, actor, **command)
