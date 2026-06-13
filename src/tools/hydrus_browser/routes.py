"""
Hydrus Browser API ルート定義

Hydrus Access Key はサーバー側で管理。
"""
import json
import logging
import os
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends, Request, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
import httpx

from .client import get_hydrus_client
from .cache import get_thumbnail_cache

logger = logging.getLogger(__name__)

# ページネーションデフォルト
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_METADATA_IDS = 256


def create_hydrus_router(require_auth) -> APIRouter:
    """Hydrus Browser API ルーターを作成"""
    router = APIRouter(prefix="/api/hydrus", tags=["hydrus"])

    @router.get("/health")
    async def health_check(_=Depends(require_auth)):
        """Hydrus Client API の接続確認"""
        try:
            client = get_hydrus_client()
            data = await client.health_check()
            return JSONResponse(content={"ok": True, **data})
        except Exception as e:
            logger.error(f"Hydrus接続エラー: {e}")
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": "Hydrus Clientに接続できません"},
            )

    @router.get("/services")
    async def get_services(_=Depends(require_auth)):
        """タグサービス一覧を取得"""
        try:
            client = get_hydrus_client()
            data = await client.get_services()
            return JSONResponse(content=data)
        except Exception as e:
            logger.error(f"Hydrusサービス取得エラー: {e}")
            raise HTTPException(status_code=502, detail="Hydrus APIエラー")

    @router.get("/search")
    async def search_files(
        tags: str = Query(..., description="JSON配列形式のタグリスト"),
        page: int = Query(1, ge=1, description="ページ番号"),
        per_page: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="1ページあたりの件数"),
        file_sort_type: Optional[int] = Query(None, description="ソートタイプ"),
        file_sort_asc: Optional[bool] = Query(None, description="昇順ソート"),
        _=Depends(require_auth),
    ):
        """タグでファイルを検索（ページネーション付き）"""
        try:
            tag_list = json.loads(tags)
            if not isinstance(tag_list, list):
                raise ValueError("tags はJSON配列である必要があります")
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"tagsパラメータが不正: {e}")

        try:
            client = get_hydrus_client()
            all_file_ids = await client.search_files(
                tags=tag_list,
                file_sort_type=file_sort_type,
                file_sort_asc=file_sort_asc,
            )

            total = len(all_file_ids)
            start = (page - 1) * per_page
            end = start + per_page
            page_ids = all_file_ids[start:end]

            return JSONResponse(content={
                "file_ids": page_ids,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": (total + per_page - 1) // per_page if total > 0 else 0,
            })
        except Exception as e:
            logger.error(f"Hydrus検索エラー: {e}")
            raise HTTPException(status_code=502, detail="Hydrus検索に失敗しました")

    @router.get("/metadata")
    async def get_metadata(
        file_ids: str = Query(..., description="JSON配列形式のfile_idリスト"),
        only_basic: bool = Query(False, description="基本情報のみ取得"),
        _=Depends(require_auth),
    ):
        """ファイルメタデータを取得"""
        try:
            id_list = json.loads(file_ids)
            if not isinstance(id_list, list):
                raise ValueError("file_ids はJSON配列である必要があります")
            if len(id_list) > MAX_METADATA_IDS:
                raise ValueError(f"一度に取得できるのは{MAX_METADATA_IDS}件までです")
            # 全てが正の整数であることを検証
            for fid in id_list:
                if not isinstance(fid, int) or fid < 1:
                    raise ValueError(f"無効なfile_id: {fid}")
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"file_idsパラメータが不正: {e}")

        try:
            client = get_hydrus_client()
            metadata = await client.get_file_metadata(
                file_ids=id_list,
                only_basic=only_basic,
            )
            return JSONResponse(content={"metadata": metadata})
        except Exception as e:
            logger.error(f"Hydrusメタデータ取得エラー: {e}")
            raise HTTPException(status_code=502, detail="メタデータ取得に失敗しました")

    @router.get("/tags/search")
    async def search_tags(
        search: str = Query(..., min_length=1, description="検索文字列"),
        tag_service_key: Optional[str] = Query(None, description="タグサービスキー"),
        _=Depends(require_auth),
    ):
        """タグオートコンプリート検索"""
        try:
            client = get_hydrus_client()
            data = await client.search_tags(search=search, tag_service_key=tag_service_key)
            return JSONResponse(content=data)
        except Exception as e:
            logger.error(f"Hydrusタグ検索エラー: {e}")
            raise HTTPException(status_code=502, detail="タグ検索に失敗しました")

    @router.get("/thumbnail/{file_id}")
    async def get_thumbnail(
        file_id: int,
        _=Depends(require_auth),
    ):
        """サムネイル画像を取得（キャッシュ付き）"""
        if file_id < 1:
            raise HTTPException(status_code=400, detail="無効なfile_id")

        # キャッシュ確認
        cache = get_thumbnail_cache()
        cached = cache.get(file_id)
        if cached:
            content_type, data = cached
            return Response(
                content=data,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )

        # Hydrusから取得
        try:
            client = get_hydrus_client()
            data, content_type = await client.get_thumbnail(file_id)
            cache.put(file_id, content_type, data)
            return Response(
                content=data,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
        except Exception as e:
            logger.error(f"Hydrusサムネイル取得エラー (file_id={file_id}): {e}")
            raise HTTPException(status_code=502, detail="サムネイル取得に失敗しました")

    @router.get("/file/{file_id}")
    async def get_file(
        file_id: int,
        _=Depends(require_auth),
    ):
        """フルサイズファイルをストリーミング配信"""
        if file_id < 1:
            raise HTTPException(status_code=400, detail="無効なfile_id")

        try:
            client = get_hydrus_client()
            resp = await client.get_file_stream(file_id)

            if resp.status_code != 200:
                await resp.aclose()
                raise HTTPException(status_code=resp.status_code, detail="ファイル取得失敗")

            content_type = resp.headers.get("content-type", "application/octet-stream")
            content_length = resp.headers.get("content-length")

            headers = {"Cache-Control": "public, max-age=3600"}
            if content_length:
                headers["Content-Length"] = content_length

            async def stream_generator():
                try:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        yield chunk
                finally:
                    await resp.aclose()

            return StreamingResponse(
                stream_generator(),
                media_type=content_type,
                headers=headers,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrusファイル取得エラー (file_id={file_id}): {e}")
            raise HTTPException(status_code=502, detail="ファイル取得に失敗しました")

    @router.get("/cache/stats")
    async def cache_stats(_=Depends(require_auth)):
        """サムネイルキャッシュの統計情報"""
        cache = get_thumbnail_cache()
        return JSONResponse(content=cache.stats)

    @router.post("/tags/edit")
    async def edit_tags(
        request: Request,
        _=Depends(require_auth),
    ):
        """ファイルのタグを追加/削除"""
        try:
            body = await request.json()
            file_ids = body.get("file_ids", [])
            service_keys_to_tags = body.get("service_keys_to_tags", {})

            if not file_ids or not isinstance(file_ids, list):
                raise HTTPException(status_code=400, detail="file_ids は必須です")
            if not service_keys_to_tags or not isinstance(service_keys_to_tags, dict):
                raise HTTPException(status_code=400, detail="service_keys_to_tags は必須です")

            client = get_hydrus_client()
            await client.add_tags(
                file_ids=file_ids,
                service_keys_to_tags=service_keys_to_tags,
            )
            return JSONResponse(content={"ok": True})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrusタグ編集エラー: {e}")
            raise HTTPException(status_code=502, detail="タグ編集に失敗しました")

    @router.post("/files/archive")
    async def archive_files(
        request: Request,
        _=Depends(require_auth),
    ):
        """ファイルをアーカイブに移動"""
        try:
            body = await request.json()
            file_ids = body.get("file_ids", [])
            if not file_ids or not isinstance(file_ids, list):
                raise HTTPException(status_code=400, detail="file_ids ���必須です")

            client = get_hydrus_client()
            await client.archive_files(file_ids=file_ids)
            return JSONResponse(content={"ok": True, "count": len(file_ids)})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrusアーカイブエラー: {e}")
            raise HTTPException(status_code=502, detail="アーカイブに失敗しました")

    @router.post("/ratings/set")
    async def set_rating(
        request: Request,
        _=Depends(require_auth),
    ):
        """ファイルのレーティングを設定"""
        try:
            body = await request.json()
            file_id = body.get("file_id")
            rating_service_key = body.get("rating_service_key")
            rating = body.get("rating")  # float or null

            if not file_id or not isinstance(file_id, int):
                raise HTTPException(status_code=400, detail="file_id は必須です")
            if not rating_service_key or not isinstance(rating_service_key, str):
                raise HTTPException(status_code=400, detail="rating_service_key は必須です")

            client = get_hydrus_client()
            await client.set_rating(
                file_id=file_id,
                rating_service_key=rating_service_key,
                rating=rating,
            )
            return JSONResponse(content={"ok": True})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrusレーティング設定エラー: {e}")
            raise HTTPException(status_code=502, detail="レーティング設定に失敗しました")

    @router.post("/files/inbox")
    async def unarchive_files(
        request: Request,
        _=Depends(require_auth),
    ):
        """ファイルをインボックスに戻す"""
        try:
            body = await request.json()
            file_ids = body.get("file_ids", [])
            if not file_ids or not isinstance(file_ids, list):
                raise HTTPException(status_code=400, detail="file_ids は必須です")

            client = get_hydrus_client()
            await client.unarchive_files(file_ids=file_ids)
            return JSONResponse(content={"ok": True, "count": len(file_ids)})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrusインボックス戻しエラー: {e}")
            raise HTTPException(status_code=502, detail="���ンボックスに戻す操作���失敗しました")

    return router


HYDRUS_COMPAT_PATHS = (
    "/api_version",
    "/session_key",
    "/get_services",
    "/get_files/search_files",
    "/get_files/file_metadata",
    "/get_files/thumbnail",
    "/get_files/file",
    "/add_tags/search_tags",
    "/add_tags/add_tags",
    "/edit_ratings/set_rating",
    "/add_files/archive_files",
    "/add_files/unarchive_files",
)

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _is_latin1_header(value: str) -> bool:
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def create_hydrus_compat_router() -> APIRouter:
    """Expose standard Hydrus Client API paths as a generic reverse proxy."""
    router = APIRouter(tags=["hydrus-compat"])

    async def forward_hydrus_request(request: Request, path: str) -> StreamingResponse:
        hydrus_url = os.environ.get("HYDRUS_API_URL", "http://127.0.0.1:45869").rstrip("/")
        target_url = f"{hydrus_url}{path}"
        body = await request.body()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
            and key.lower() not in {"host", "content-length", "cookie", "authorization"}
        }

        if not any(
            key.lower() == "hydrus-client-api-access-key" for key in headers
        ):
            access_key = os.environ.get("HYDRUS_ACCESS_KEY", "")
            if access_key:
                headers["Hydrus-Client-API-Access-Key"] = access_key

        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        try:
            outbound = client.build_request(
                request.method,
                target_url,
                params=request.query_params,
                headers=headers,
                content=body,
            )
            upstream = await client.send(outbound, stream=True)
        except Exception:
            await client.aclose()
            raise

        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
            and _is_latin1_header(value)
        }

        async def close_upstream() -> None:
            await upstream.aclose()
            await client.aclose()

        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=upstream.headers.get("content-type"),
            background=BackgroundTask(close_upstream),
        )

    def add_proxy_route(path: str) -> None:
        async def endpoint(request: Request) -> StreamingResponse:
            return await forward_hydrus_request(request, path)

        router.add_api_route(
            path,
            endpoint,
            methods=["GET", "POST"],
            include_in_schema=False,
        )

    for compat_path in HYDRUS_COMPAT_PATHS:
        add_proxy_route(compat_path)

    return router
