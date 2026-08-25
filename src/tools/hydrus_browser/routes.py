"""
Hydrus Browser API ルート定義

Hydrus Access Key はサーバー側で管理。
"""
import inspect
import json
import logging
import os
import hashlib
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Mapping, Optional, List

from fastapi import APIRouter, HTTPException, Depends, Request, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx

from .client import get_hydrus_client
from .cache import get_thumbnail_cache
from .credentials import load_hydrus_credentials, validate_hydrus_api_url

logger = logging.getLogger(__name__)

# ページネーションデフォルト
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_METADATA_IDS = 256
PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


async def _await_maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def create_hydrus_router(
    require_auth,
    *,
    get_current_user: Optional[Callable[[Request], Any]] = None,
    get_hydrus_credentials: Optional[Callable[[str], Any]] = None,
) -> APIRouter:
    """Hydrus Browser API ルーターを作成"""
    router = APIRouter(prefix="/api/hydrus", tags=["hydrus"])

    async def current_user_id(request: Request) -> str:
        user: Any = None
        if get_current_user is not None:
            user = await _await_maybe(get_current_user(request))
        if isinstance(user, Mapping):
            raw = user.get("id") or user.get("user_id")
        else:
            raw = user
        # Only trust forwarded identity when the internal proxy has validated
        # its shared key.  Direct clients cannot select another user by header.
        if not raw:
            internal = request.headers.get("x-internal-auth")
            expected = os.environ.get("INTERNAL_API_KEY", "")
            if internal and expected and internal == expected:
                raw = request.headers.get("x-forwarded-user-id")
        if not raw:
            raise HTTPException(status_code=401, detail="ユーザーを特定できません")
        return str(raw)

    async def user_client(request: Request):
        user_id = await current_user_id(request)
        loader = get_hydrus_credentials or load_hydrus_credentials
        credentials = await _await_maybe(loader(user_id))
        if not isinstance(credentials, Mapping):
            raise HTTPException(status_code=503, detail="Hydrus接続が設定されていません")
        api_url = credentials.get("api_url") or credentials.get("apiUrl")
        access_key = credentials.get("access_key") or credentials.get("accessKey")
        if not isinstance(api_url, str) or not isinstance(access_key, str) or not api_url or not access_key:
            raise HTTPException(status_code=503, detail="Hydrus接続が設定されていません")
        safe_api_url = await validate_hydrus_api_url(api_url)
        if safe_api_url is None:
            raise HTTPException(status_code=503, detail="Hydrus API URLが許可されていません")
        # Scope includes the authenticated principal and an integration
        # fingerprint.  If a user replaces their Hydrus endpoint/key, stale
        # thumbnails from the previous integration cannot collide on file_id.
        fingerprint = hashlib.sha256(
            f"{safe_api_url}\x00{access_key}".encode("utf-8")
        ).hexdigest()[:24]
        scope = f"{user_id}:{fingerprint}"
        return get_hydrus_client(api_url=safe_api_url, access_key=access_key), scope

    @asynccontextmanager
    async def resolved_client(request: Request):
        """Resolve one principal's client and always close its HTTP session."""
        client, scope = await user_client(request)
        try:
            yield client, scope
        finally:
            await client.close()

    @router.get("/health")
    async def health_check(request: Request, _=Depends(require_auth)):
        """Hydrus Client API の接続確認"""
        try:
            async with resolved_client(request) as (client, _scope):
                data = await client.health_check()
            return JSONResponse(content={"ok": True, **data}, headers=PRIVATE_HEADERS)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrus接続エラー: {e}")
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": "Hydrus Clientに接続できません"},
                headers=PRIVATE_HEADERS,
            )

    @router.get("/services")
    async def get_services(request: Request, _=Depends(require_auth)):
        """タグサービス一覧を取得"""
        try:
            async with resolved_client(request) as (client, _scope):
                data = await client.get_services()
            return JSONResponse(content=data, headers=PRIVATE_HEADERS)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrusサービス取得エラー: {e}")
            raise HTTPException(status_code=502, detail="Hydrus APIエラー")

    @router.get("/search")
    async def search_files(
        request: Request,
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
            async with resolved_client(request) as (client, _scope):
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
            }, headers=PRIVATE_HEADERS)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrus検索エラー: {e}")
            raise HTTPException(status_code=502, detail="Hydrus検索に失敗しました")

    @router.get("/metadata")
    async def get_metadata(
        request: Request,
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
            async with resolved_client(request) as (client, _scope):
                metadata = await client.get_file_metadata(
                    file_ids=id_list,
                    only_basic=only_basic,
                )
            return JSONResponse(content={"metadata": metadata}, headers=PRIVATE_HEADERS)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrusメタデータ取得エラー: {e}")
            raise HTTPException(status_code=502, detail="メタデータ取得に失敗しました")

    @router.get("/tags/search")
    async def search_tags(
        request: Request,
        search: str = Query(..., min_length=1, description="検索文字列"),
        tag_service_key: Optional[str] = Query(None, description="タグサービスキー"),
        _=Depends(require_auth),
    ):
        """タグオートコンプリート検索"""
        try:
            async with resolved_client(request) as (client, _scope):
                data = await client.search_tags(search=search, tag_service_key=tag_service_key)
            return JSONResponse(content=data, headers=PRIVATE_HEADERS)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrusタグ検索エラー: {e}")
            raise HTTPException(status_code=502, detail="タグ検索に失敗しました")

    @router.get("/thumbnail/{file_id}")
    async def get_thumbnail(
        request: Request,
        file_id: int,
        _=Depends(require_auth),
    ):
        """サムネイル画像を取得（キャッシュ付き）"""
        if file_id < 1:
            raise HTTPException(status_code=400, detail="無効なfile_id")

        # キャッシュ確認
        cache = get_thumbnail_cache()
        try:
            async with resolved_client(request) as (client, scope):
                cached = cache.get(scope, file_id)
                if cached:
                    content_type, data = cached
                else:
                    # Hydrusから取得
                    data, content_type = await client.get_thumbnail(file_id)
                    cache.put(scope, file_id, content_type, data)
                return Response(
                    content=data,
                    media_type=content_type,
                    headers={"Cache-Control": "private, no-store"},
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrusサムネイル取得エラー (file_id={file_id}): {e}")
            raise HTTPException(status_code=502, detail="サムネイル取得に失敗しました")

    @router.get("/file/{file_id}")
    async def get_file(
        request: Request,
        file_id: int,
        _=Depends(require_auth),
    ):
        """フルサイズファイルをストリーミング配信"""
        if file_id < 1:
            raise HTTPException(status_code=400, detail="無効なfile_id")

        client = None
        try:
            client, _scope = await user_client(request)
            resp = await client.get_file_stream(file_id)

            if resp.status_code != 200:
                await resp.aclose()
                await client.close()
                client = None
                raise HTTPException(status_code=resp.status_code, detail="ファイル取得失敗")

            content_type = resp.headers.get("content-type", "application/octet-stream")
            content_length = resp.headers.get("content-length")
            content_encoding = resp.headers.get("content-encoding")

            headers = {"Cache-Control": "private, no-store"}
            # httpx yields decoded bytes from ``aiter_bytes``.  Do not forward
            # an encoded Content-Length to a downstream client, otherwise a
            # compressed upstream response advertises a stale body size.
            if content_length and not content_encoding:
                headers["Content-Length"] = content_length

            async def stream_generator():
                try:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.close()
                    # The stream owns the client after StreamingResponse is
                    # returned; avoid a second close in any outer error path.

            return StreamingResponse(
                stream_generator(),
                media_type=content_type,
                headers=headers,
            )
        except HTTPException:
            if client is not None:
                await client.close()
            raise
        except Exception as e:
            if client is not None:
                await client.close()
            logger.error(f"Hydrusファイル取得エラー (file_id={file_id}): {e}")
            raise HTTPException(status_code=502, detail="ファイル取得に失敗しました")

    @router.get("/cache/stats")
    async def cache_stats(request: Request, _=Depends(require_auth)):
        # Resolve the principal even for statistics so a caller cannot infer
        # another user's cache state; only that principal's scoped entries are
        # counted.
        user_id = await current_user_id(request)
        """サムネイルキャッシュの統計情報"""
        cache = get_thumbnail_cache()
        return JSONResponse(content=cache.stats_for(user_id), headers=PRIVATE_HEADERS)

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

            async with resolved_client(request) as (client, _scope):
                await client.add_tags(
                    file_ids=file_ids,
                    service_keys_to_tags=service_keys_to_tags,
                )
            return JSONResponse(content={"ok": True}, headers=PRIVATE_HEADERS)
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

            async with resolved_client(request) as (client, _scope):
                await client.archive_files(file_ids=file_ids)
            return JSONResponse(content={"ok": True, "count": len(file_ids)}, headers=PRIVATE_HEADERS)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrusアーカイブエラー: {e}")
            raise HTTPException(status_code=502, detail="アーカイブに失敗しました")

    @router.post("/files/delete")
    async def delete_files(
        request: Request,
        _=Depends(require_auth),
    ):
        """ファイルを削除（Hydrus のゴミ箱へ送る）"""
        try:
            body = await request.json()
            file_ids = body.get("file_ids", [])
            reason = body.get("reason")
            if not file_ids or not isinstance(file_ids, list):
                raise HTTPException(status_code=400, detail="file_ids は必須です")

            async with resolved_client(request) as (client, _scope):
                await client.delete_files(file_ids=file_ids, reason=reason)
            return JSONResponse(content={"ok": True, "count": len(file_ids)}, headers=PRIVATE_HEADERS)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrus削除エラー: {e}")
            raise HTTPException(status_code=502, detail="削除に失敗しました")

    @router.post("/files/undelete")
    async def undelete_files(
        request: Request,
        _=Depends(require_auth),
    ):
        """削除したファイルを元に戻す"""
        try:
            body = await request.json()
            file_ids = body.get("file_ids", [])
            if not file_ids or not isinstance(file_ids, list):
                raise HTTPException(status_code=400, detail="file_ids は必須です")

            async with resolved_client(request) as (client, _scope):
                await client.undelete_files(file_ids=file_ids)
            return JSONResponse(content={"ok": True, "count": len(file_ids)}, headers=PRIVATE_HEADERS)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Hydrus削除取り消しエラー: {e}")
            raise HTTPException(status_code=502, detail="削除の取り消しに失敗しました")

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

            async with resolved_client(request) as (client, _scope):
                await client.set_rating(
                    file_id=file_id,
                    rating_service_key=rating_service_key,
                    rating=rating,
                )
            return JSONResponse(content={"ok": True}, headers=PRIVATE_HEADERS)
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

            async with resolved_client(request) as (client, _scope):
                await client.unarchive_files(file_ids=file_ids)
            return JSONResponse(content={"ok": True, "count": len(file_ids)}, headers=PRIVATE_HEADERS)
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

# Only representation/transport headers are forwarded to Hydrus.  In
# particular, never pass AoiTalk's internal identity headers, cookies, bearer
# tokens, or a client-provided Hydrus session/access key through the proxy.
COMPAT_FORWARD_HEADERS = {
    "accept",
    "accept-encoding",
    "content-type",
    "range",
    "if-none-match",
    "if-modified-since",
    "user-agent",
}

# Only the representation/range metadata needed by the browser is copied
# from Hydrus.  Never expose Set-Cookie, Location, auth challenges, or other
# upstream/security headers through the compatibility proxy.
COMPAT_RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "content-disposition",
    "etag",
    "last-modified",
}


def _is_latin1_header(value: str) -> bool:
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def create_hydrus_compat_router(
    *,
    require_auth=None,
    get_current_user: Optional[Callable[[Request], Any]] = None,
    get_hydrus_credentials: Optional[Callable[[str], Any]] = None,
) -> APIRouter:
    """Expose standard Hydrus Client API paths as a generic reverse proxy."""
    if require_auth is None:
        async def require_auth() -> None:
            raise HTTPException(status_code=500, detail="Hydrus proxy auth is not configured")

    router = APIRouter(tags=["hydrus-compat"])

    async def compat_user_id(request: Request) -> str:
        if get_current_user is not None:
            user = await _await_maybe(get_current_user(request))
            raw = user.get("id") if isinstance(user, Mapping) else user
            if raw:
                return str(raw)
        internal = request.headers.get("x-internal-auth")
        expected = os.environ.get("INTERNAL_API_KEY", "")
        forwarded = request.headers.get("x-forwarded-user-id")
        if internal and expected and internal == expected and forwarded:
            return forwarded
        raise HTTPException(status_code=401, detail="ユーザーを特定できません")

    async def forward_hydrus_request(request: Request, path: str) -> StreamingResponse:
        user_id = await compat_user_id(request)
        loader = get_hydrus_credentials or load_hydrus_credentials
        credentials = await _await_maybe(loader(user_id))
        if not isinstance(credentials, Mapping):
            raise HTTPException(status_code=503, detail="Hydrus接続が設定されていません")
        hydrus_url = credentials.get("api_url") or credentials.get("apiUrl")
        access_key = credentials.get("access_key") or credentials.get("accessKey")
        if not isinstance(hydrus_url, str) or not isinstance(access_key, str):
            raise HTTPException(status_code=503, detail="Hydrus接続が設定されていません")
        hydrus_url = await validate_hydrus_api_url(hydrus_url)
        if hydrus_url is None:
            raise HTTPException(status_code=503, detail="Hydrus API URLが許可されていません")
        target_url = f"{hydrus_url}{path}"
        body = await request.body()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in COMPAT_FORWARD_HEADERS
        }

        # Client-supplied Hydrus keys are ignored; use the encrypted key owned
        # by the authenticated AoiTalk principal.
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
            if key.lower() in COMPAT_RESPONSE_HEADERS
            and _is_latin1_header(value)
        }
        response_headers["cache-control"] = "private, no-store"

        async def stream_upstream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_upstream(),
            status_code=upstream.status_code,
            headers=response_headers,
            media_type=upstream.headers.get("content-type"),
        )

    def add_proxy_route(path: str) -> None:
        async def endpoint(
            request: Request,
            _auth=Depends(require_auth),
        ) -> StreamingResponse:
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
