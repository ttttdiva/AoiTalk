"""
Hydrus Client API への非同期HTTPクライアント（読み取り専用）
"""
import os
import json
import logging
from typing import Optional, List, Dict, Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:45869"
SESSION_KEY_HEADER = "Hydrus-Client-API-Session-Key"
ACCESS_KEY_HEADER = "Hydrus-Client-API-Access-Key"


class HydrusBrowserClient:
    """Hydrus Client API への読み取り専用プロキシクライアント"""

    def __init__(
        self,
        api_url: Optional[str] = None,
        access_key: Optional[str] = None,
    ):
        self._api_url = (
            api_url
            or os.environ.get("HYDRUS_API_URL", DEFAULT_API_URL)
        ).rstrip("/")
        self._access_key = access_key or os.environ.get("HYDRUS_ACCESS_KEY", "")
        self._session_key: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

        if not self._access_key:
            logger.warning("HYDRUS_ACCESS_KEY が未設定です")

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
            logger.error(f"セッションキー取得失敗: {resp.status_code}")
        except Exception as e:
            logger.error(f"セッションキー取得エラー: {e}")
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

        resp = await client.post(
            f"{self._api_url}{endpoint}",
            headers=self._get_headers(),
            json=json_data,
        )

        if resp.status_code == 419:
            logger.info("Hydrusセッション期限切れ、再取得中...")
            if await self._refresh_session_key():
                resp = await client.post(
                    f"{self._api_url}{endpoint}",
                    headers=self._get_headers(),
                    json=json_data,
                )

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

        resp = await client.get(
            f"{self._api_url}{endpoint}",
            headers=self._get_headers(),
            params=params,
        )

        # セッション期限切れ → リフレッシュしてリトライ
        if resp.status_code == 419:
            logger.info("Hydrusセッション期限切れ、再取得中...")
            if await self._refresh_session_key():
                resp = await client.get(
                    f"{self._api_url}{endpoint}",
                    headers=self._get_headers(),
                    params=params,
                )

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
        resp = await client.send(req, stream=True)

        if resp.status_code == 419:
            await resp.aclose()
            if await self._refresh_session_key():
                req = client.build_request(
                    "GET",
                    f"{self._api_url}{endpoint}",
                    headers=self._get_headers(),
                    params=params,
                )
                resp = await client.send(req, stream=True)

        return resp

    # ── 公開API ──

    async def health_check(self) -> Dict[str, Any]:
        """Hydrus API バージョン確認"""
        resp = await self._request("/api_version")
        resp.raise_for_status()
        return resp.json()

    async def get_services(self) -> Dict[str, Any]:
        """タグサービス一覧を取得"""
        resp = await self._request("/get_services")
        resp.raise_for_status()
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
        resp.raise_for_status()
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
        resp.raise_for_status()
        data = resp.json()
        return data.get("metadata", [])

    async def search_tags(self, search: str, tag_service_key: Optional[str] = None) -> Dict[str, Any]:
        """タグオートコンプリート検索"""
        params: Dict[str, Any] = {"search": search}
        if tag_service_key:
            params["tag_service_key"] = tag_service_key

        resp = await self._request("/add_tags/search_tags", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_thumbnail(self, file_id: int) -> tuple[bytes, str]:
        """サムネイル画像を取得。(data, content_type) を返す。"""
        resp = await self._request("/get_files/thumbnail", params={"file_id": file_id})
        resp.raise_for_status()
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
        resp.raise_for_status()

    async def archive_files(self, file_ids: List[int]) -> None:
        """ファイルをアーカイブに移動（インボックスから削除）"""
        resp = await self._post_request(
            "/add_files/archive_files",
            json_data={"file_ids": file_ids},
        )
        resp.raise_for_status()

    async def unarchive_files(self, file_ids: List[int]) -> None:
        """ファイルをインボックスに戻す（アーカイブから削除）"""
        resp = await self._post_request(
            "/add_files/unarchive_files",
            json_data={"file_ids": file_ids},
        )
        resp.raise_for_status()

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
        resp.raise_for_status()

    async def close(self) -> None:
        """クライアントを閉じる"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
            self._session_key = None


# モジュールレベルのシングルトン
_hydrus_client: Optional[HydrusBrowserClient] = None


def get_hydrus_client() -> HydrusBrowserClient:
    global _hydrus_client
    if _hydrus_client is None:
        _hydrus_client = HydrusBrowserClient()
    return _hydrus_client
