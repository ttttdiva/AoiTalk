"""Authenticated metadata commands and separately streamed asset bytes."""

from __future__ import annotations

from typing import Annotated, Any, Callable
from uuid import UUID
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from .media_asset_schemas import (
    MediaAssetCapabilitiesResponse,
    MediaAssetRegisterRequest,
    MediaAssetResponse,
    MediaAssetSourceResponse,
    MediaProductArtifactRequest,
    MediaProductArtifactResponse,
    MediaProductFilesResponse,
    MediaProductFileImportRequest,
    MediaProductFileArtifact,
    MediaProductDeliverableResponse,
)
from .media_operations_routes import (
    _actor,
    _invoke,
    _principal_projection,
    _with_session,
)
from .routes.storage_stream import byte_range
from .http_errors import build_storage_unavailable_detail
from ..services.media_asset_service import (
    IMAGE_MAX_BYTES,
    VIDEO_MAX_BYTES,
    MIME_EXTENSIONS,
    MediaAssetService,
)
from ..services.storage_io import StorageError
from ..services.media_product_asset_service import MediaProductAssetService
from ..services.media_product_file_service import MediaProductFileService
from ..services.artifact_snapshot_storage import stream_snapshot
from .operations_routes import MediaActionCreateRequest, ActionResponse
from ..services.operations_service import OperationsError


def create_media_asset_router(
    get_db_manager: Callable[[], Any],
    get_user_from_request: Callable[..., Any],
    require_auth_dependency: Callable[..., Any],
    *,
    asset_service: MediaAssetService | None = None,
):
    router = APIRouter(tags=["operations-media"])
    service = asset_service or MediaAssetService()
    products = MediaProductAssetService(assets=service)
    files = MediaProductFileService(assets=service)

    async def actor(request):
        return _principal_projection(await _actor(get_user_from_request, request))

    async def call(method, principal, *args, **kwargs):
        try:
            return await _with_session(
                get_db_manager,
                lambda session: _invoke(method, session, principal, *args, **kwargs),
            )
        except StorageError as exc:
            detail = (
                build_storage_unavailable_detail(exc.code)
                if exc.status_code == 503
                else None
            )
            raise HTTPException(
                exc.status_code, detail=detail or exc.detail()
            ) from None
        except OperationsError as exc:
            raise HTTPException(exc.status_code, detail=str(exc)) from None

    @router.get(
        "/assets/capabilities",
        response_model=MediaAssetCapabilitiesResponse,
        operation_id="media_asset_capabilities",
    )
    async def capabilities(request: Request, _: Any = Depends(require_auth_dependency)):
        await actor(request)
        return {
            "image_max_bytes": IMAGE_MAX_BYTES,
            "video_max_bytes": VIDEO_MAX_BYTES,
            "supported_mime_types": list(MIME_EXTENSIONS),
            "upload_transport": "binary_stream",
        }

    @router.get(
        "/assets",
        response_model=list[MediaAssetResponse],
        operation_id="media_list_assets",
    )
    async def list_assets(
        request: Request,
        persona_id: UUID,
        limit: int = Query(100, ge=1, le=100),
        _: Any = Depends(require_auth_dependency),
    ):
        return await call(service.list_assets, await actor(request), persona_id, limit)

    @router.get(
        "/asset-sources",
        response_model=list[MediaAssetSourceResponse],
        operation_id="media_list_asset_sources",
    )
    async def list_sources(
        request: Request,
        response: Response,
        persona_id: UUID,
        limit: int = Query(100, ge=1, le=100),
        _: Any = Depends(require_auth_dependency),
    ):
        unavailable_roots: set[str] = set()
        entries = await call(
            service.list_sources,
            await actor(request),
            persona_id,
            limit,
            unavailable_roots=unavailable_roots,
        )
        if unavailable_roots:
            response.headers["X-Media-Asset-Sources-Partial"] = "true"
            response.headers["X-Media-Asset-Sources-Warning"] = "storage_unavailable"
            response.headers["Access-Control-Expose-Headers"] = (
                "X-Media-Asset-Sources-Partial, X-Media-Asset-Sources-Warning"
            )
        return entries

    @router.post(
        "/assets/register",
        response_model=MediaAssetResponse,
        operation_id="media_register_asset",
    )
    async def register(
        payload: MediaAssetRegisterRequest,
        request: Request,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ],
        _: Any = Depends(require_auth_dependency),
    ):
        return await call(
            service.register,
            await actor(request),
            **payload.model_dump(),
            idempotency_key=idempotency_key,
        )

    @router.post(
        "/assets/upload",
        response_model=MediaAssetResponse,
        operation_id="media_upload_asset",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    mime: {"schema": {"type": "string", "format": "binary"}}
                    for mime in MIME_EXTENSIONS
                },
            }
        },
    )
    async def upload(
        request: Request,
        persona_id: UUID,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
        ],
        content_type: Annotated[str, Header(alias="Content-Type")],
        content_item_id: UUID | None = None,
        expected_content_item_hash: str | None = Query(None, pattern=r"^[0-9a-f]{64}$"),
        expected_sha256: Annotated[
            str | None, Header(alias="X-Asset-SHA256", pattern=r"^[0-9a-f]{64}$")
        ] = None,
        _: Any = Depends(require_auth_dependency),
    ):
        if content_type not in MIME_EXTENSIONS:
            raise HTTPException(
                415, "対応する画像/動画 MIME の binary stream を指定してください"
            )
        values = request.headers.getlist("content-length")
        limit = (
            IMAGE_MAX_BYTES if content_type.startswith("image/") else VIDEO_MAX_BYTES
        )
        if values:
            if len(values) != 1 or not values[0].isdigit():
                raise HTTPException(400, "Content-Length が無効です")
            if int(values[0]) > limit:
                raise HTTPException(413, "素材がアップロード上限を超えています")
        return await call(
            service.upload,
            await actor(request),
            persona_id=persona_id,
            content_item_id=content_item_id,
            expected_content_item_hash=expected_content_item_hash,
            stream=request.stream(),
            mime_type=content_type,
            expected_sha256=expected_sha256,
            idempotency_key=idempotency_key,
        )

    @router.get(
        "/assets/{generation_output_id}",
        response_model=MediaAssetResponse,
        operation_id="media_get_asset",
    )
    async def get_asset(
        generation_output_id: UUID,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ):
        return await call(service.get_asset, await actor(request), generation_output_id)

    @router.post(
        "/assets/{generation_output_id}/product-artifact",
        response_model=MediaProductArtifactResponse,
        operation_id="media_bind_product_artifact",
    )
    async def bind_product_artifact(
        generation_output_id: UUID,
        payload: MediaProductArtifactRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ):
        return await call(
            products.bind,
            await actor(request),
            generation_output_id,
            **payload.model_dump(),
        )

    @router.post(
        "/product-actions",
        response_model=ActionResponse,
        operation_id="media_prepare_product_action",
    )
    async def prepare_product_action(
        payload: MediaActionCreateRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ):
        return await call(
            products.prepare_action,
            await actor(request),
            **payload.model_dump(exclude_none=True),
        )

    @router.get(
        "/product-files",
        response_model=MediaProductFilesResponse,
        operation_id="media_list_product_files",
    )
    async def product_files(
        request: Request,
        persona_id: UUID,
        limit: int = Query(100, ge=1, le=100),
        file_name: str = Query("", max_length=255),
        _: Any = Depends(require_auth_dependency),
    ):
        return await call(
            files.list_files, await actor(request), persona_id, limit, file_name
        )

    @router.post(
        "/product-files/import",
        response_model=MediaProductFileArtifact,
        operation_id="media_import_product_file",
    )
    async def import_product_file(
        payload: MediaProductFileImportRequest,
        request: Request,
        _: Any = Depends(require_auth_dependency),
    ):
        return await call(
            files.import_file,
            await actor(request),
            disconnected=request.is_disconnected,
            **payload.model_dump(),
        )

    @router.get(
        "/product-actions/{action_id}/deliverable",
        response_model=MediaProductDeliverableResponse,
        operation_id="media_product_deliverable_manifest",
    )
    async def product_deliverable(
        action_id: UUID,
        request: Request,
        expected_action_version: int = Query(ge=1),
        expected_payload_hash: str = Query(pattern=r"^[0-9a-f]{64}$"),
        _: Any = Depends(require_auth_dependency),
    ):
        result, _snapshot = await call(
            files.deliverable,
            await actor(request),
            action_id,
            expected_action_version,
            expected_payload_hash,
        )
        return result

    @router.get(
        "/product-actions/{action_id}/deliverable/content",
        operation_id="media_stream_product_deliverable",
        responses={
            200: {
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                }
            },
            206: {"description": "Verified partial deliverable bytes"},
            412: {"description": "Immutable version/hash mismatch"},
            416: {"description": "Unsatisfiable Range"},
        },
    )
    async def product_deliverable_content(
        action_id: UUID,
        request: Request,
        expected_action_version: int = Query(ge=1),
        expected_payload_hash: str = Query(pattern=r"^[0-9a-f]{64}$"),
        expected_artifact_id: UUID = Query(),
        expected_sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
        _: Any = Depends(require_auth_dependency),
    ):
        result, snapshot = await call(
            files.deliverable,
            await actor(request),
            action_id,
            expected_action_version,
            expected_payload_hash,
        )
        version = result["manifest"]["deliverable"]
        etag = f'"{snapshot.sha256}"'
        if (
            version["artifact_version_id"] != str(expected_artifact_id)
            or snapshot.sha256 != expected_sha256
            or request.headers.get("if-match") not in (None, etag)
        ):
            raise HTTPException(
                412, "配布物の固定版が一致しません。manifestを再取得してください"
            )
        start, end, partial = byte_range(
            request.headers.get("range"), snapshot.size_bytes
        )
        headers = {
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "X-Artifact-SHA256": snapshot.sha256,
            "X-Manifest-SHA256": result["manifest_sha256"],
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(version['filename'], safe='')}",
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{snapshot.size_bytes}"
        return StreamingResponse(
            stream_snapshot(snapshot, start, end),
            status_code=206 if partial else 200,
            media_type=version["mime_type"],
            headers=headers,
        )

    @router.get(
        "/assets/{generation_output_id}/preview",
        operation_id="media_preview_asset",
        responses={
            200: {
                "content": {
                    mime: {"schema": {"type": "string", "format": "binary"}}
                    for mime in MIME_EXTENSIONS
                }
            },
            206: {"description": "Partial asset bytes"},
            416: {"description": "Unsatisfiable or unsupported Range"},
        },
    )
    async def preview(
        generation_output_id: UUID,
        request: Request,
        expected_sha256: str | None = Query(None, pattern=r"^[0-9a-f]{64}$"),
        _: Any = Depends(require_auth_dependency),
    ):
        revision, root, relative, identity = await call(
            service.prepare_preview,
            await actor(request),
            generation_output_id,
            expected_sha256,
        )
        start, end, partial = byte_range(
            request.headers.get("range"), revision.size_bytes
        )
        etag = f'"{revision.sha256}"'
        if request.headers.get("if-match") not in (None, etag):
            raise HTTPException(412, "素材 revision が一致しません")
        headers = {
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "ETag": etag,
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f'inline; filename="asset{MIME_EXTENSIONS[revision.mime_type]}"',
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{revision.size_bytes}"
        return StreamingResponse(
            service.stream_preview(root, relative, identity, start, end),
            status_code=206 if partial else 200,
            media_type=revision.mime_type,
            headers=headers,
        )

    return router
