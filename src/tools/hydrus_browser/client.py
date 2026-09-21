"""Hydrus Client APIへの非同期HTTPクライアント。

クライアントは認証主体ごとに生成する。以前のmodule-global singletonは
ユーザー間で接続先・session keyを共有してしまうため廃止した。
"""
import json
import logging
from typing import Optional, List, Dict, Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:45869"
SESSION_KEY_HEADER = "Hydrus-Client-API-Session-Key"
ACCESS_KEY_HEADER = "Hydrus-Client-API-Access-Key"


HYDRUS_ERROR_MESSAGES: dict[str, str] = {
    "hydrus_not_configured": "Hydrus接続が設定されていません。Settingsで接続を設定してください",
    "hydrus_legacy_owner_ambiguous": "既存のHydrus設定を安全に移行できません。現在のユーザーとして明示的に取り込んでください",
    "hydrus_legacy_owner_conflict": "既存のHydrus設定の所有者を確認できません",
    "hydrus_endpoint_policy_rejected": "Hydrus API URLが許可されていません",
    "hydrus_endpoint_resolution_failed": "Hydrus API URLの名前解決に失敗しました",
    "hydrus_credential_unreadable": "Hydrus接続設定を読み取れません",
    "hydrus_credential_store_unavailable": "Hydrus接続設定を一時的に読み取れません",
    "hydrus_auth_failed": "Hydrus Clientの認証または権限を確認してください",
    "hydrus_unreachable": "Hydrus Clientに接続できません",
    "hydrus_upstream_error": "Hydrus APIエラー",
}


class HydrusClientError(RuntimeError):
    """Secret-free, typed failure from the Hydrus upstream."""

    code = "hydrus_upstream_error"
    status_code = 502

    def __init__(self, _message: str | None = None):
        # Never include ``httpx`` exception text: it can contain a URL with
        # userinfo or proxy credentials.  The static message is safe for API
        # responses and logs.
        self.message = HYDRUS_ERROR_MESSAGES[self.code]
        super().__init__(self.message)


class HydrusAuthenticationError(HydrusClientError):
    code = "hydrus_auth_failed"


class HydrusUnreachableError(HydrusClientError):
    code = "hydrus_unreachable"


class HydrusUpstreamError(HydrusClientError):
    code = "hydrus_upstream_error"


def hydrus_error_detail(error_or_code: HydrusClientError | str) -> dict[str, str]:
    """Build the allowlisted HTTP detail for a Hydrus client failure."""

    code = (
        error_or_code.code
        if isinstance(error_or_code, HydrusClientError)
        else str(error_or_code)
    )
    if code not in HYDRUS_ERROR_MESSAGES:
        code = "hydrus_upstream_error"
    return {
        "category": "hydrus",
        "code": code,
        "message": HYDRUS_ERROR_MESSAGES[code],
    }


def hydrus_error_for_status(status_code: int) -> HydrusClientError:
    """Classify one upstream status without exposing its response body."""

    if status_code in {401, 403, 419}:
        return HydrusAuthenticationError()
    return HydrusUpstreamError()


def raise_for_hydrus_response(response: httpx.Response) -> None:
    """Raise a typed error for an unsuccessful Hydrus response."""

    if response.is_error:
        raise hydrus_error_for_status(response.status_code)


class HydrusBrowserClient:
    """Hydrus Client API への読み取り専用プロキシクライアント"""

    def __init__(
        self,
        api_url: Optional[str] = None,
        access_key: Optional[str] = None,
    ):
        # Do not silently fall back to process-global HYDRUS_* credentials.  The
        # caller has already resolved the authenticated user's integration.
        self._api_url = (api_url or "").rstrip("/")
        self._access_key = access_key or ""
        self._session_key: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

        if not self._access_key:
            logger.warning("Hydrus access key が未設定です")

    async def _ensure_client(self) -> httpx.AsyncClient:
        """httpx クライアントの遅延初期化"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        return self._client

    def _get_headers(self) -> Dict[str, str]:
        """認証ヘッダーを返す（セッションキー優先）"""
        if self._session_key:
            return {SESSION_KEY_HEADER: self._session_key}
        return {ACCESS_KEY_HEADER: self._access_key}

    async def _refresh_session_key(self) -> bool:
        """セッションキーを取得/更新"""
        try:
            client = await self._ensure_client()
            resp = await client.get(
                f"{self._api_url}/session_key",
                headers={ACCESS_KEY_HEADER: self._access_key},
            )
            if resp.status_code == 200:
                data = resp.json()
                self._session_key = data.get("session_key")
                logger.info("Hydrusセッションキー取得成功")
                return True
            # A bad access key is reported by the real request as an auth
            # failure.  Keep this refresh probe non-throwing so callers can
            # still classify the final endpoint response consistently.
            logger.warning("Hydrusセッションキー取得失敗 (status=%s)", resp.status_code)
        except httpx.RequestError:
            logger.warning("Hydrusセッションキー取得時にHydrusへ到達できません")
        except (ValueError, TypeError):
            logger.warning("Hydrusセッションキー応答を解釈できません")
        except Exception:
            logger.warning("Hydrusセッションキー取得に失敗しました")
        return False

    async def _post_request(
        self,
        endpoint: str,
        json_data: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """Hydrus APIへのPOSTリクエスト。419(セッション期限切れ)時は自動再取得。"""
        client = await self._ensure_client()

        if not self._session_key:
            await self._refresh_session_key()

        try:
            resp = await client.post(
                f"{self._api_url}{endpoint}",
                headers=self._get_headers(),
                json=json_data,
            )
        except httpx.RequestError:
            raise HydrusUnreachableError() from None

        if resp.status_code == 419:
            logger.info("Hydrusセッション期限切れ、再取得中...")
            if await self._refresh_session_key():
                try:
                    resp = await client.post(
                        f"{self._api_url}{endpoint}",
                        headers=self._get_headers(),
                        json=json_data,
                    )
                except httpx.RequestError:
                    raise HydrusUnreachableError() from None

        return resp

    async def _request(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """Hydrus APIへのGETリクエスト。419(セッション期限切れ)時は自動再取得。"""
        client = await self._ensure_client()

        # セッションキー未取得なら初回取得
        if not self._session_key:
            await self._refresh_session_key()

        try:
            resp = await client.get(
                f"{self._api_url}{endpoint}",
                headers=self._get_headers(),
                params=params,
            )
        except httpx.RequestError:
            raise HydrusUnreachableError() from None

        # セッション期限切れ → リフレッシュしてリトライ
        if resp.status_code == 419:
            logger.info("Hydrusセッション期限切れ、再取得中...")
            if await self._refresh_session_key():
                try:
                    resp = await client.get(
                        f"{self._api_url}{endpoint}",
                        headers=self._get_headers(),
                        params=params,
                    )
                except httpx.RequestError:
                    raise HydrusUnreachableError() from None

        return resp

    async def _stream_request(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """ストリーミングGETリクエスト（大きなファイル用）"""
        client = await self._ensure_client()

        if not self._session_key:
            await self._refresh_session_key()

        req = client.build_request(
            "GET",
            f"{self._api_url}{endpoint}",
            headers=self._get_headers(),
            params=params,
        )
        try:
            resp = await client.send(req, stream=True)
        except httpx.RequestError:
            raise HydrusUnreachableError() from None

        if resp.status_code == 419:
            await resp.aclose()
            if await self._refresh_session_key():
                req = client.build_request(
                    "GET",
                    f"{self._api_url}{endpoint}",
                    headers=self._get_headers(),
                    params=params,
                )
                try:
                    resp = await client.send(req, stream=True)
                except httpx.RequestError:
                    raise HydrusUnreachableError() from None

        return resp

    # ── 公開API ──

    async def health_check(self) -> Dict[str, Any]:
        """Hydrus API バージョン確認"""
        resp = await self._request("/api_version")
        raise_for_hydrus_response(resp)
        return resp.json()

    async def get_services(self) -> Dict[str, Any]:
        """タグサービス一覧を取得"""
        resp = await self._request("/get_services")
        raise_for_hydrus_response(resp)
        return resp.json()

    async def search_files(
        self,
        tags: List[str],
        file_sort_type: Optional[int] = None,
        file_sort_asc: Optional[bool] = None,
        file_service_key: Optional[str] = None,
        tag_service_key: Optional[str] = None,
    ) -> List[int]:
        """タグ検索してfile_idリストを返す"""
        params: Dict[str, Any] = {
            "tags": json.dumps(tags),
        }
        if file_sort_type is not None:
            params["file_sort_type"] = file_sort_type
        if file_sort_asc is not None:
            params["file_sort_asc"] = json.dumps(file_sort_asc)
        if file_service_key:
            params["file_service_key"] = file_service_key
        if tag_service_key:
            params["tag_service_key"] = tag_service_key

        resp = await self._request("/get_files/search_files", params=params)
        raise_for_hydrus_response(resp)
        data = resp.json()
        return data.get("file_ids", [])

    async def get_file_metadata(
        self,
        file_ids: List[int],
        only_basic: bool = False,
    ) -> List[Dict[str, Any]]:
        """ファイルメタデータを取得"""
        params: Dict[str, Any] = {
            "file_ids": json.dumps(file_ids),
        }
        if only_basic:
            params["only_return_basic_information"] = json.dumps(True)

        resp = await self._request("/get_files/file_metadata", params=params)
        raise_for_hydrus_response(resp)
        data = resp.json()
        return data.get("metadata", [])

    async def search_tags(self, search: str, tag_service_key: Optional[str] = None) -> Dict[str, Any]:
        """タグオートコンプリート検索"""
        params: Dict[str, Any] = {"search": search}
        if tag_service_key:
            params["tag_service_key"] = tag_service_key

        resp = await self._request("/add_tags/search_tags", params=params)
        raise_for_hydrus_response(resp)
        return resp.json()

    async def get_thumbnail(self, file_id: int) -> tuple[bytes, str]:
        """サムネイル画像を取得。(data, content_type) を返す。"""
        resp = await self._request("/get_files/thumbnail", params={"file_id": file_id})
        raise_for_hydrus_response(resp)
        content_type = resp.headers.get("content-type", "image/jpeg")
        return resp.content, content_type

    async def get_file_stream(self, file_id: int) -> httpx.Response:
        """ファイルをストリーミングで取得（呼び出し元でcloseすること）"""
        return await self._stream_request("/get_files/file", params={"file_id": file_id})

    async def add_tags(
        self,
        file_ids: List[int],
        service_keys_to_tags: Dict[str, Dict[str, List[str]]],
    ) -> None:
        """ファイルにタグを追加/削除する

        service_keys_to_tags: {
            service_key: {
                "0": ["tag1", "tag2"],   # add
                "1": ["tag3"],           # delete
            }
        }
        """
        payload = {
            "file_ids": file_ids,
            "service_keys_to_tags": service_keys_to_tags,
        }
        resp = await self._post_request("/add_tags/add_tags", json_data=payload)
        raise_for_hydrus_response(resp)

    async def archive_files(self, file_ids: List[int]) -> None:
        """ファイルをアーカイブに移動（インボックスから削除）"""
        resp = await self._post_request(
            "/add_files/archive_files",
            json_data={"file_ids": file_ids},
        )
        raise_for_hydrus_response(resp)

    async def unarchive_files(self, file_ids: List[int]) -> None:
        """ファイルをインボックスに戻す（アーカイブから削除）"""
        resp = await self._post_request(
            "/add_files/unarchive_files",
            json_data={"file_ids": file_ids},
        )
        raise_for_hydrus_response(resp)

    async def delete_files(
        self, file_ids: List[int], reason: Optional[str] = None
    ) -> None:
        """ファイルをゴミ箱へ送る（Hydrus の delete_files）"""
        payload: Dict[str, Any] = {"file_ids": file_ids}
        if reason:
            payload["reason"] = reason
        resp = await self._post_request(
            "/add_files/delete_files",
            json_data=payload,
        )
        raise_for_hydrus_response(resp)

    async def undelete_files(self, file_ids: List[int]) -> None:
        """削除したファイルを元に戻す（Hydrus の undelete_files）"""
        resp = await self._post_request(
            "/add_files/undelete_files",
            json_data={"file_ids": file_ids},
        )
        raise_for_hydrus_response(resp)

    async def set_rating(
        self,
        file_id: int,
        rating_service_key: str,
        rating: Optional[float],
    ) -> None:
        """ファイルのレーティングを設定する（nullでクリア）"""
        payload: Dict[str, Any] = {
            "file_id": file_id,
            "rating_service_key": rating_service_key,
        }
        if rating is not None:
            payload["rating"] = rating
        else:
            payload["rating"] = None
        resp = await self._post_request(
            "/edit_ratings/set_rating",
            json_data=payload,
        )
        raise_for_hydrus_response(resp)

    async def close(self) -> None:
        """クライアントを閉じる"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
            self._session_key = None


def get_hydrus_client(
    *,
    api_url: Optional[str] = None,
    access_key: Optional[str] = None,
) -> HydrusBrowserClient:
    """Create an isolated client for one resolved integration.

    ``api_url`` and ``access_key`` are intentionally required by callers in
    production; omitting either yields a client that fails closed instead of
    reading global environment credentials.
    """

    return HydrusBrowserClient(api_url=api_url, access_key=access_key)
