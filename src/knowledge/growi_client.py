"""GROWI 社内Wiki への読み取り専用 REST クライアント。

GROWI（https://growi.org/）はページ本文を Markdown で保持し、REST API を公開する。
本クライアントは RAG 取り込みのために以下だけを行う:

1. ページ一覧の列挙（パス・ページID・リビジョンID・更新日時）
2. 個別ページの Markdown 本文取得

認証は GROWI の個人設定で発行する API トークン（access token）を用いる。
トークンは ``access_token`` クエリと ``Authorization: Bearer`` ヘッダの両方で送り、
GROWI のバージョン差（classic v1 / v3）双方に追従する。

対象は GROWI v6 以降の REST API を主とし、v3 が失敗した場合は classic API に
フォールバックする。エンドポイントは ``endpoint_overrides`` で差し替え可能。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 20.0
# 1回の列挙で取得する最大ページ数。GROWI 既定の上限に合わせる。
_LIST_PAGE_SIZE = 100
# 列挙ループの安全上限（無限ループ防止）。100 * 1000 = 10万ページまで。
_MAX_LIST_ITERATIONS = 1000


class GrowiClientError(RuntimeError):
    """GROWI への接続・応答に関する失敗。"""


@dataclass(frozen=True)
class GrowiPage:
    """列挙で得たページのメタデータ。本文は含まない。"""

    page_id: str
    path: str
    revision_id: Optional[str] = None
    updated_at: Optional[str] = None

    @property
    def change_key(self) -> str:
        """差分検知に使う安定キー。リビジョンID優先、無ければ更新日時。"""
        return self.revision_id or self.updated_at or ""


@dataclass
class GrowiClient:
    base_url: str
    api_token: str
    timeout: float = _DEFAULT_TIMEOUT
    endpoint_overrides: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.base_url:
            raise GrowiClientError("GROWI のベースURLが空です")
        if not self.api_token:
            raise GrowiClientError("GROWI の API トークンが空です")

    # ------------------------------------------------------------------
    # 低レベル HTTP
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_token}",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, Any],
    ) -> Any:
        merged = {"access_token": self.api_token, **params}
        url = self._url(path)
        try:
            response = await client.get(url, params=merged, headers=self._headers())
        except httpx.HTTPError as exc:
            raise GrowiClientError(f"GROWI への接続に失敗しました ({url}): {exc}") from exc
        if response.status_code == 401:
            raise GrowiClientError("GROWI 認証に失敗しました (401)。API トークンを確認してください")
        if response.status_code == 403:
            raise GrowiClientError("GROWI へのアクセスが拒否されました (403)。トークンの権限を確認してください")
        if response.status_code >= 400:
            raise GrowiClientError(
                f"GROWI が {response.status_code} を返しました ({path})"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise GrowiClientError(f"GROWI の応答が JSON ではありません ({path})") from exc

    # ------------------------------------------------------------------
    # 接続テスト
    # ------------------------------------------------------------------
    async def test_connection(self) -> dict[str, Any]:
        """疎通とトークンの有効性を軽く確認する。"""
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            endpoint = self.endpoint_overrides.get("list", "/_api/v3/pages/list")
            try:
                data = await self._get_json(client, endpoint, {"path": "/", "limit": 1})
                pages = self._extract_pages(data)
                return {"ok": True, "sample_count": len(pages)}
            except GrowiClientError:
                # v3 が無い古い GROWI 向けに classic API で再確認。
                data = await self._get_json(
                    client, "/_api/pages.list", {"path": "/", "limit": 1}
                )
                pages = self._extract_pages(data)
                return {"ok": True, "sample_count": len(pages), "api": "classic"}

    # ------------------------------------------------------------------
    # ページ列挙
    # ------------------------------------------------------------------
    async def list_pages(self, root_path: str = "/") -> list[GrowiPage]:
        """root_path 配下の全ページを列挙する。"""
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            try:
                return await self._list_pages_v3(client, root_path)
            except GrowiClientError as exc:
                logger.warning("GROWI v3 列挙に失敗、classic API で再試行: %s", exc)
                return await self._list_pages_classic(client, root_path)

    async def _list_pages_v3(
        self, client: httpx.AsyncClient, root_path: str
    ) -> list[GrowiPage]:
        endpoint = self.endpoint_overrides.get("list", "/_api/v3/pages/list")
        collected: dict[str, GrowiPage] = {}
        page_number = 1
        for _ in range(_MAX_LIST_ITERATIONS):
            data = await self._get_json(
                client,
                endpoint,
                {"path": root_path, "limit": _LIST_PAGE_SIZE, "page": page_number},
            )
            pages = self._extract_pages(data)
            if not pages:
                break
            for item in pages:
                parsed = self._parse_page_item(item)
                if parsed:
                    collected[parsed.page_id] = parsed
            if len(pages) < _LIST_PAGE_SIZE:
                break
            page_number += 1
        return list(collected.values())

    async def _list_pages_classic(
        self, client: httpx.AsyncClient, root_path: str
    ) -> list[GrowiPage]:
        data = await self._get_json(
            client, "/_api/pages.list", {"path": root_path, "limit": 1000}
        )
        collected: dict[str, GrowiPage] = {}
        for item in self._extract_pages(data):
            parsed = self._parse_page_item(item)
            if parsed:
                collected[parsed.page_id] = parsed
        return list(collected.values())

    # ------------------------------------------------------------------
    # 本文取得
    # ------------------------------------------------------------------
    async def get_page_body(self, page: GrowiPage) -> str:
        """ページの Markdown 本文を取得する。"""
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            endpoint = self.endpoint_overrides.get("page", "/_api/v3/page")
            try:
                data = await self._get_json(client, endpoint, {"pageId": page.page_id})
                body = self._extract_body(data)
                if body is not None:
                    return body
            except GrowiClientError as exc:
                logger.debug("GROWI v3 本文取得に失敗、classic で再試行: %s", exc)
            data = await self._get_json(
                client, "/_api/pages.get", {"page_id": page.page_id}
            )
            body = self._extract_body(data)
            return body or ""

    # ------------------------------------------------------------------
    # 応答パース（GROWI のバージョン差を吸収）
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_pages(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        for key in ("pages", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _parse_page_item(item: dict[str, Any]) -> Optional[GrowiPage]:
        page_id = item.get("_id") or item.get("id") or item.get("pageId")
        path = item.get("path")
        if not page_id or not path:
            return None
        revision = item.get("revision")
        revision_id: Optional[str] = None
        if isinstance(revision, dict):
            revision_id = revision.get("_id") or revision.get("id")
        elif isinstance(revision, str):
            revision_id = revision
        return GrowiPage(
            page_id=str(page_id),
            path=str(path),
            revision_id=str(revision_id) if revision_id else None,
            updated_at=item.get("updatedAt") or item.get("updated_at"),
        )

    @staticmethod
    def _extract_body(data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        page = data.get("page")
        if not isinstance(page, dict):
            # 一部バージョンは page を包まずに直接返す。
            page = data
        revision = page.get("revision")
        if isinstance(revision, dict):
            body = revision.get("body")
            if isinstance(body, str):
                return body
        body = page.get("body")
        return body if isinstance(body, str) else None


def build_page_url(base_url: str, page_path: str) -> str:
    """GROWI ページの閲覧URLを組み立てる（パスはURLエンコード）。"""
    base = base_url.rstrip("/")
    raw = page_path if page_path.startswith("/") else f"/{page_path}"
    encoded = quote(raw, safe="/")
    return f"{base}{encoded}"
