"""外部AoiTalkサーバーへのHTTPプロキシクライアント。

接続プロファイル（URL + 復号済みトークン）を受け取り、リモートの公開APIへ
Bearer 認証付きで中継する。リモートから取得したデータ本体は永続化せず、
短TTLのプロセス内メモリキャッシュにのみ保持する。

書き込み系操作は、事前に capabilities を確認して機能の有効性と書き込み可否を
判断してから実行する（会社版スナップショットの仕様乖離対策）。
"""

import logging
import asyncio
import ipaddress
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

# 接続テスト・読み取りのデフォルトタイムアウト（秒）。
_DEFAULT_TIMEOUT = 10.0
# capabilities キャッシュの存続時間（秒）。短TTLで仕様乖離の追従性を保つ。
_CAPABILITIES_TTL = 60.0


class RemoteConnectorError(RuntimeError):
    """リモート接続・応答に関する失敗。"""


@dataclass(frozen=True)
class RemoteRawResponse:
    """ファイル中継用のレスポンススナップショット。"""

    status_code: int
    headers: Dict[str, str]
    content: bytes


class RemoteServerConnector:
    """1つの外部AoiTalkサーバーに対する中継クライアント。"""

    def __init__(
        self,
        base_url: str,
        auth_token: Optional[str],
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._timeout = timeout
        self._capabilities_cache: Optional[Tuple[float, Dict[str, Any]]] = None

    @staticmethod
    def _private_targets_allowed() -> bool:
        return os.getenv("AOITALK_ALLOW_PRIVATE_REMOTE_SERVERS", "").strip().lower() in {
            "1", "true", "yes", "on"
        }

    async def _ensure_safe_network_target(self) -> None:
        """Prevent a saved hostname from being used for SSRF into private networks."""
        if self._private_targets_allowed():
            return
        try:
            parsed = urlsplit(self._base_url)
            hostname = parsed.hostname
            if parsed.scheme not in {"http", "https"} or not hostname:
                raise RemoteConnectorError("remote base URL must be an HTTP(S) URL")
            if parsed.username or parsed.password:
                raise RemoteConnectorError("remote base URL must not contain credentials")
            lowered = hostname.lower().rstrip(".")
            if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".localhost"):
                raise RemoteConnectorError("private remote server hosts are disabled")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except (ValueError, socket.gaierror, OSError) as exc:
            raise RemoteConnectorError(f"remote host cannot be resolved safely: {exc}") from exc
        for result in addresses:
            try:
                address = ipaddress.ip_address(result[4][0])
            except ValueError as exc:
                raise RemoteConnectorError("remote host resolved to an invalid address") from exc
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
                or not address.is_global
            ):
                raise RemoteConnectorError("private remote server hosts are disabled")

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        await self._ensure_safe_network_target()
        url = self._url(path)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise RemoteConnectorError(f"request to {url} failed: {exc}") from exc
        if response.status_code == 401:
            raise RemoteConnectorError("remote authentication failed (401)")
        if response.status_code >= 400:
            raise RemoteConnectorError(
                f"remote returned {response.status_code} for {method} {path}"
            )
        try:
            return response.json()
        except ValueError:
            return None

    async def request_raw(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> RemoteRawResponse:
        """JSONに変換せず、許可済みファイル応答を中継する。"""
        await self._ensure_safe_network_target()
        url = self._url(path)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise RemoteConnectorError(f"request to {url} failed: {exc}") from exc
        if response.status_code == 401:
            raise RemoteConnectorError("remote authentication failed (401)")
        if response.status_code >= 400:
            raise RemoteConnectorError(
                f"remote returned {response.status_code} for {method} {path}"
            )
        allowed_headers = {
            "content-type",
            "content-disposition",
            "content-length",
            "accept-ranges",
            "content-range",
        }
        return RemoteRawResponse(
            status_code=response.status_code,
            headers={
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in allowed_headers
            },
            content=response.content,
        )

    async def fetch_capabilities(self, use_cache: bool = True) -> Dict[str, Any]:
        """リモートの capabilities を取得する（短TTLキャッシュ付き）。"""
        now = time.monotonic()
        if (
            use_cache
            and self._capabilities_cache is not None
            and now - self._capabilities_cache[0] < _CAPABILITIES_TTL
        ):
            return self._capabilities_cache[1]
        data = await self._request("GET", "/api/capabilities")
        if not isinstance(data, dict):
            raise RemoteConnectorError("capabilities response was not an object")
        self._capabilities_cache = (now, data)
        return data

    async def test_connection(self) -> Dict[str, Any]:
        """接続テストを行い、capabilities を返す。

        認証・到達性・応答形式をまとめて検証する。失敗時は
        RemoteConnectorError を送出する。
        """
        return await self.fetch_capabilities(use_cache=False)

    async def _ensure_writable(self, feature: Optional[str] = None) -> None:
        """書き込み前に capabilities を確認する。

        ``feature`` を指定した場合、その feature flag が有効でなければ
        RemoteConnectorError を送出する。
        """
        capabilities = await self.fetch_capabilities()
        if feature is not None:
            features = capabilities.get("features") or {}
            resources = capabilities.get("resources") or {}
            resource = resources.get(feature)
            resource_write = (
                resource.get("write") if isinstance(resource, dict) else None
            )
            if resource_write is False or (
                resource_write is None and not features.get(feature, False)
            ):
                raise RemoteConnectorError(
                    f"remote feature '{feature}' is disabled; write not allowed"
                )

    async def get(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        """リモートの読み取りエンドポイントを中継する。"""
        return await self._request("GET", path, params=params)

    async def write(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        required_feature: Optional[str] = None,
    ) -> Any:
        """書き込み系操作を中継する。事前に capabilities を確認する。"""
        await self._ensure_writable(required_feature)
        return await self._request(method, path, json_body=json_body)
