"""Metadata-only wire contract; upload bytes use a separate stream."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AssetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MediaAssetRegisterRequest(AssetModel):
    persona_id: UUID
    content_item_id: UUID | None = None
    expected_content_item_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    source_kind: Literal["generated_media", "library_file", "generation_output"]
    source_id: UUID
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class MediaAssetResponse(AssetModel):
    asset_revision_id: UUID
    generation_output_id: UUID
    persona_id: UUID
    content_item_id: UUID | None
    owner_user_id: UUID
    project_id: UUID | None
    source_kind: Literal[
        "upload", "generated_media", "library_file", "generation_output"
    ]
    source_id: UUID | None
    sha256: str
    mime_type: str
    size_bytes: int
    revision_hash: str
    preview_url: str
    created_at: datetime


class MediaAssetSourceResponse(AssetModel):
    source_kind: Literal["generated_media", "library_file", "generation_output"]
    source_id: UUID
    mime_type: str
    size_bytes: int | None
    sha256: str | None


class MediaAssetCapabilitiesResponse(AssetModel):
    image_max_bytes: int
    video_max_bytes: int
    supported_mime_types: list[str]
    upload_transport: Literal["binary_stream"]


class MediaProductArtifactRequest(AssetModel):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class MediaProductArtifactResponse(AssetModel):
    generation_output_id: UUID
    asset_revision_id: UUID
    artifact_version_id: UUID
    owner_user_id: UUID
    project_id: UUID | None
    revision_hash: str
    sha256: str
    mime_type: str
    size_bytes: int


class MediaProductFile(AssetModel):
    source_id: UUID
    filename: str
    owner_user_id: UUID
    project_id: UUID | None
    mime_type: str
    size_bytes: int
    etag: str


class MediaProductFilesResponse(AssetModel):
    max_bytes: int
    files: list[MediaProductFile]


class MediaProductFileImportRequest(AssetModel):
    persona_id: UUID
    source_id: UUID
    expected_etag: str = Field(min_length=1, max_length=255)
    expected_size_bytes: int = Field(gt=0, le=1024**3)


class MediaProductFileArtifact(AssetModel):
    artifact_version_id: UUID
    filename: str | None
    owner_user_id: UUID
    project_id: UUID | None
    mime_type: str
    size_bytes: int
    sha256: str


class MediaProductDeliverableManifest(AssetModel):
    schema_version: Literal["product-deliverable/v1"]
    action: dict[str, str | int]
    source: dict[str, str | int | None]
    deliverable: MediaProductFileArtifact
    transport: Literal["verified_chunk_stream"]
    max_bytes: int


class MediaProductDeliverableResponse(AssetModel):
    manifest: MediaProductDeliverableManifest
    manifest_json: str
    manifest_sha256: str
