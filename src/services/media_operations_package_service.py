"""Read-only, revision-bound MediaOps packages from verified internal artifacts.

GenerationOutput is joined only through the managed asset's explicit
ArtifactVersion ID; no URL, provider ID or hash search guesses its bytes.
A verified package says nothing about
quality, rights, approval, submission or publication success.
"""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import zipfile
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import select

from ..memory.models import ArtifactVersion, ExternalAction
from ..memory.models.media_assets import MediaAssetRevision
from ..memory.models.media_operations_generation import GenerationOutput
from .media_asset_service import _binding as asset_binding
from .media_product_asset_service import product_artifact_binding_hash
from ..memory.models.media_operations_content import ContentVariant, ContentVariantRevision
from .media_operations_content_service import _normalize_payload, canonical_publication_payload
from .operations_service import (
    MEDIA_ACTION_TYPES,
    OperationsAuthorizationError,
    OperationsConflictError,
    OperationsNotFoundError,
    OperationsService,
    OperationsValidationError,
    _media_revision_content_hash,
    canonical_json,
    sha256_json,
)

MAX_TOTAL_ASSET_BYTES = 25 * 1024 * 1024
MAX_ASSETS = 100
MAX_MANIFEST_BYTES = 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MIME = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*\Z")
_REFERENCE_FIELDS = (
    "preview_assets", "thumbnail_assets", "media", "media_asset",
    "attachments", "thumbnail", "cover",
)
_SOURCE_IDS = (
    "content_item_id", "content_variant_id", "content_variant_revision_id",
    "persona_revision_id", "platform_account_id", "platform_account_revision_id",
)
_SOURCE_HASHES = (
    "content_item_hash", "content_variant_hash", "content_variant_revision_hash",
    "persona_revision_hash", "platform_account_revision_hash",
)


def _uuid(value: Any) -> str | None:
    if not isinstance(value, (str, UUID)):
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _artifact_ref(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _uuid(value.removeprefix("artifact:"))


def _same_scope(action: Any, row: Any) -> None:
    if row.owner_user_id != action.owner_user_id or row.project_id != action.project_id:
        raise OperationsAuthorizationError("package source owner/project scope mismatch")


def _filename(value: Any, artifact_id: str) -> str:
    name = value if isinstance(value, str) and value else f"{artifact_id}.bin"
    # Flat names only. Reject rather than silently changing a filename that
    # carries the artifact's intended extension and meaning.
    stem = name.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³")
    if (
        name in {".", ".."} or name != name.strip() or name.endswith(".")
        or len(name.encode("utf-8")) > 240 or stem in reserved
        or any(char in '<>:"/\\|?*' or unicodedata.category(char).startswith("C") for char in name)
    ):
        raise OperationsConflictError("package artifact filename is unsafe")
    return name


async def _row(operations: OperationsService, session: Any, model: Any, row_id: Any) -> Any:
    parsed = _uuid(row_id)
    if parsed is None:
        raise OperationsConflictError("package source identity is invalid")
    # Refresh the identity map on every read. PostgreSQL's shared lock also
    # keeps revisions from changing while this request is collecting bytes.
    value = await operations._scalar(
        session,
        select(model).where(model.id == UUID(parsed)).limit(1)
        .execution_options(populate_existing=True).with_for_update(read=True),
    )
    if value is None:
        raise OperationsNotFoundError("package source not found")
    return value


async def _snapshot(
    session: Any, actor: Any, action_id: Any, version: int, digest: str,
    operations: OperationsService,
) -> tuple[Any, dict[str, Any], dict[str, Any], Any]:
    action = await _row(operations, session, ExternalAction, action_id)
    await operations._assert_entity_access(session, actor, action, permission="read")
    if action.action_type not in MEDIA_ACTION_TYPES:
        raise OperationsValidationError("package downloads require a MediaOps action")
    raw = action.payload_json
    if (
        int(action.action_version) != version or action.payload_hash != digest
        or not isinstance(raw, Mapping) or sha256_json(raw) != digest
        or raw.get("platform") != action.platform
        or raw.get("action_type") != action.action_type
        or raw.get("operation_key") != action.action_type
        or raw.get("artifact_hashes") != action.artifact_hashes
    ):
        raise OperationsConflictError("package action version or canonical payload changed")
    if not isinstance(action.artifact_hashes, list) or any(
        not isinstance(pin, str) or not _HASH.fullmatch(pin) for pin in action.artifact_hashes
    ):
        raise OperationsConflictError("package action artifact hashes are invalid")
    source: dict[str, Any] = {}
    for key in _SOURCE_IDS:
        actual = getattr(action, key)
        expected = _uuid(raw.get(key))
        if (actual is not None and expected is None) or expected != (str(actual) if actual else None):
            raise OperationsConflictError("package action source binding changed")
        source[key] = expected
    for key in _SOURCE_HASHES:
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not _HASH.fullmatch(value)):
            raise OperationsConflictError("package source hash is invalid")
        source[key] = value
    revision = await _row(operations, session, ContentVariantRevision, action.content_variant_revision_id)
    variant = await _row(operations, session, ContentVariant, action.content_variant_id)
    for value in (revision, variant):
        await operations._assert_entity_access(session, actor, value, permission="read")
        _same_scope(action, value)
    try:
        payload_matches = (
            canonical_json(canonical_publication_payload(revision.payload_json, action.platform))
            == canonical_json(canonical_publication_payload(raw.get("payload"), action.platform))
        )
    except ValueError as exc:
        raise OperationsConflictError("package typed payload is invalid") from exc
    if (
        revision.content_variant_id != variant.id
        or variant.content_item_id != action.content_item_id
        or variant.platform != action.platform or revision.platform != action.platform
        or variant.create_hash != source["content_variant_hash"]
        or revision.content_hash != source["content_variant_revision_hash"]
        or _media_revision_content_hash(revision) != revision.content_hash
        or not payload_matches
        or revision.version != raw.get("content_variant_revision_version")
    ):
        raise OperationsConflictError("package immutable source revision changed")
    for key in ("content_item_id", "persona_revision_id", "platform_account_id", "platform_account_revision_id"):
        if getattr(revision, key) != getattr(action, key):
            raise OperationsConflictError("package source revision identity changed")
    for key in ("content_item_hash", "persona_revision_hash", "platform_account_revision_hash"):
        if getattr(revision, key) != source[key]:
            raise OperationsConflictError("package source revision hash changed")
    latest_version = await operations._scalar(
        session, select(ContentVariantRevision.version)
        .where(ContentVariantRevision.content_variant_id == variant.id)
        .order_by(ContentVariantRevision.version.desc()).limit(1),
    )
    if latest_version != revision.version:
        raise OperationsConflictError("package source revision is stale; revise the action")
    source["content_variant_revision_version"] = revision.version
    try:
        typed, _ = _normalize_payload(raw.get("payload"), action.platform)
    except ValueError as exc:
        raise OperationsConflictError("package typed payload is invalid") from exc
    # The closed normalizer never projects raw provider/connection/credential
    # objects. An opaque deliverable reference may itself contain a URL/path;
    # only the supported internal reference form is safe to export.
    if "deliverable_package_ref" in typed:
        artifact_id = _artifact_ref(typed["deliverable_package_ref"])
        typed["deliverable_package_ref"] = f"artifact:{artifact_id}" if artifact_id else None
    return action, source, typed, revision


def _references(typed: Mapping[str, Any], raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if "deliverable_package_ref" in raw:
        artifact_id = _artifact_ref(raw["deliverable_package_ref"])
        result.append({"role": "deliverable", "reference_type": "artifact" if artifact_id else "unsupported",
                       **({"reference_id": artifact_id} if artifact_id else {})})
    for role in _REFERENCE_FIELDS:
        for reference in typed.get(role, []):
            result.append({"role": role, "reference_type": "generation_output", "reference_id": reference})
    for post_index, post in enumerate(typed.get("posts", []), 1):
        for reference in post.get("media", []):
            result.append({"role": f"posts.{post_index}.media", "reference_type": "generation_output", "reference_id": reference})
    if len(result) > MAX_ASSETS:
        raise OperationsConflictError("package exceeds its 100 asset limit")
    return result


async def build_action_package(
    session: Any, actor: Any, action_id: Any, *, expected_action_version: int,
    expected_payload_hash: str, operations: OperationsService | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Build a deterministic ZIP without creating events, artifacts or receipts.

    The 25 MiB limit applies to included asset bytes. The manifest contributes
    at most an additional 1 MiB, plus bounded ZIP framing. Missing/unsupported
    bytes produce a partial manifest; authorization/integrity errors fail closed.
    """
    if (
        isinstance(expected_action_version, bool) or not isinstance(expected_action_version, int)
        or expected_action_version < 1 or not isinstance(expected_payload_hash, str)
        or not _HASH.fullmatch(expected_payload_hash) or _uuid(action_id) is None
    ):
        raise OperationsValidationError("expected action version, SHA-256 payload hash and action ID are required")
    operations = operations or OperationsService()
    # A GET must not flush unrelated pending ORM changes either.
    with session.no_autoflush:
        action, source, typed, revision = await _snapshot(
            session, actor, action_id, expected_action_version, expected_payload_hash, operations,
        )
        action_binding = (action.owner_user_id, action.project_id)
        raw_typed = action.payload_json["payload"]
        pins = action.artifact_hashes or []
        output_pins = {
            str(item.get("generation_output_id")): item.get("sha256")
            for item in (revision.generation_output_refs_json or []) if isinstance(item, Mapping)
        }
        assets: list[dict[str, Any]] = []
        reasons: list[dict[str, Any]] = []
        files: list[tuple[str, bytes]] = []
        total = 0
        names: dict[str, str] = {}
        included: dict[str, dict[str, Any]] = {}
        for ordinal, reference in enumerate(_references(typed, raw_typed), 1):
            asset = {"ordinal": ordinal, **reference, "status": "unavailable"}
            reason: str | None = None
            if reference["reference_type"] == "unsupported":
                reason = "unsupported_artifact_reference"
            elif reference["reference_type"] == "generation_output":
                reason = "generation_output_artifact_binding_unavailable"
                pin = output_pins.get(reference["reference_id"])
                if isinstance(pin, str) and _HASH.fullmatch(pin):
                    asset["sha256"] = pin
                # Join by the exact GenerationOutput FK and persisted artifact ID.
                # External/unbound outputs stay partial; scope/integrity errors fail.
                binding = await operations._scalar(session, select(MediaAssetRevision)
                    .where(MediaAssetRevision.generation_output_id == UUID(reference["reference_id"]))
                    .execution_options(populate_existing=True).with_for_update(read=True))
                if binding is not None and binding.artifact_version_id is not None:
                    output = await _row(operations, session, GenerationOutput, reference["reference_id"])
                    for value in (binding, output):
                        _same_scope(action, value)
                        await operations._assert_entity_access(session, actor, value, permission="read")
                    if (output.byte_binding_state != "managed" or pin != output.sha256
                        or binding.sha256 != pin or binding.mime_type != output.mime_type
                        or binding.revision_hash != sha256_json(asset_binding(binding))
                        or binding.artifact_binding_hash != product_artifact_binding_hash(binding, binding.artifact_version_id)):
                        raise OperationsConflictError("package managed asset binding changed")
                    asset.update(artifact_id=str(binding.artifact_version_id))
                    reason = None
            if reference["reference_type"] == "artifact" or asset.get("artifact_id"):
                artifact_id = asset.get("artifact_id", reference["reference_id"])
                if artifact_id in included:
                    if reference["reference_type"] == "generation_output" and (
                        included[artifact_id]["sha256"] != pin or included[artifact_id]["mime_type"] != binding.mime_type
                        or included[artifact_id]["size_bytes"] != binding.size_bytes
                    ):
                        raise OperationsConflictError("package reused artifact does not match managed asset")
                    asset.update(included[artifact_id])
                    assets.append(asset)
                    continue
                try:
                    artifact = await _row(operations, session, ArtifactVersion, artifact_id)
                    _same_scope(action, artifact)
                    await operations._assert_entity_access(session, actor, artifact, permission="read")
                    name = _filename(artifact.filename, artifact_id)
                    identity = unicodedata.normalize("NFKC", name).casefold()
                    if identity in names and names[identity] != artifact_id:
                        raise OperationsConflictError("package contains colliding filenames")
                    names[identity] = artifact_id
                    size = artifact.size_bytes
                    if (not isinstance(size, int) or size < 0 or not _HASH.fullmatch(str(artifact.sha256))
                        or not isinstance(artifact.mime_type, str) or not _MIME.fullmatch(artifact.mime_type)):
                        raise OperationsConflictError("package artifact metadata is invalid")
                    if reference["reference_type"] == "generation_output" and (
                        artifact.sha256 != pin or artifact.mime_type != binding.mime_type
                        or size != binding.size_bytes
                    ):
                        raise OperationsConflictError("package artifact does not match managed asset")
                    if artifact.sha256 not in pins:
                        reason = "artifact_hash_not_bound_to_action"
                    elif total + size > MAX_TOTAL_ASSET_BYTES:
                        reason = "package_total_size_exceeded"
                    else:
                        metadata, data = await operations.get_artifact_content(session, actor, artifact_id)
                        if (metadata.get("id") != artifact_id or metadata.get("sha256") != artifact.sha256
                            or len(data) != size or hashlib.sha256(data).hexdigest() != artifact.sha256):
                            raise OperationsConflictError("package artifact content changed")
                        path = f"assets/{name}"
                        files.append((path, data))
                        total += size
                        asset.update(status="included", artifact_id=artifact_id, filename=name,
                                     archive_path=path, extension=PurePosixPath(name).suffix,
                                     mime_type=artifact.mime_type, size_bytes=size, sha256=artifact.sha256)
                        included[artifact_id] = {key: asset[key] for key in (
                            "status", "artifact_id", "filename", "archive_path", "extension", "mime_type", "size_bytes", "sha256")}
                except OperationsNotFoundError:
                    reason = "artifact_content_unavailable"
            if reason:
                asset["reason"] = reason
                reasons.append({"code": reason, "role": asset["role"], "ordinal": ordinal})
            assets.append(asset)
        receipt_projection: dict[str, Any] = {
            "action_id": str(action.id),
            "action_version": expected_action_version,
            "payload_hash": expected_payload_hash,
        }
        manifest: dict[str, Any] = {
            "schema_version": "mediaops.action-package.v1",
            "action": {"id": str(action.id), "action_version": expected_action_version,
                       "payload_hash": expected_payload_hash, "platform": action.platform,
                       "action_type": action.action_type},
            "source": source, "payload": typed, "assets": assets,
            "status": "partial" if reasons else "verified", "reasons": reasons,
            "receipt": receipt_projection,
            "limits": {"max_total_bytes": MAX_TOTAL_ASSET_BYTES, "asset_bytes": total,
                       "max_assets": MAX_ASSETS, "max_manifest_bytes": MAX_MANIFEST_BYTES},
        }
        manifest_bytes = canonical_json(manifest).encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise OperationsConflictError("package manifest exceeds its 1 MiB limit")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in [("manifest.json", manifest_bytes), *files]:
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, data)
        final_action, final_source, final_typed, _ = await _snapshot(
            session, actor, action_id, expected_action_version, expected_payload_hash, operations,
        )
        if (action_binding != (final_action.owner_user_id, final_action.project_id)
            or final_source != source or final_typed != typed):
            raise OperationsConflictError("package source changed during download")
        return manifest, buffer.getvalue()
