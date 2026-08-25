"""Heartbeat webhook 用の SSRF 安全 HTTP クライアント。"""
from __future__ import annotations

import asyncio
import http.client
import json
import ssl
from typing import Any, Optional
from urllib.parse import urljoin

import certifi
import httpx

from ..services.url_ingest_service import UrlIngestService


async def safe_webhook_request(
    method: str,
    url: str,
    *,
    json_payload: Optional[Any] = None,
    timeout_seconds: float = 30.0,
) -> httpx.Response:
    """公開 URL のみへ IP ピン留めした HTTP リクエストを送る。"""
    del timeout_seconds  # pinned request uses fixed connect/read timeouts
    current = url
    normalized_method = str(method or "POST").upper()
    for _ in range(8):
        parts, address = await UrlIngestService._resolve_public_url(current)
        response = await asyncio.to_thread(
            _pinned_request,
            parts,
            address,
            method=normalized_method,
            json_payload=json_payload,
        )
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                return response
            current = urljoin(current, location)
            continue
        return response
    raise ValueError("リダイレクト回数が上限を超えました")


def _pinned_request(
    parts,
    address: str,
    *,
    method: str,
    json_payload: Optional[Any],
) -> httpx.Response:
    host = str(parts.hostname)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    body = b""
    headers: dict[str, str] = {
        "Host": host if parts.port is None else f"{host}:{parts.port}",
        "User-Agent": "AoiTalk-Heartbeat/1.0",
        "Accept": "application/json",
    }
    if json_payload is not None:
        body = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(body))

    if parts.scheme == "https":
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())

        class PinnedHttpsConnection(http.client.HTTPSConnection):
            def connect(self):
                import socket

                sock = socket.create_connection((address, port), timeout=25)
                self.sock = self._context.wrap_socket(sock, server_hostname=host)

        connection = PinnedHttpsConnection(
            host,
            port=port,
            timeout=25,
            context=context,
        )
    else:
        connection = http.client.HTTPConnection(address, port=port, timeout=25)

    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.endheaders(body if body else None)
        raw = connection.getresponse()
        content = raw.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("レスポンス本文がサイズ上限を超えました")
        return httpx.Response(
            raw.status,
            headers=dict(raw.getheaders()),
            content=content,
            request=httpx.Request(method, parts.geturl()),
        )
    finally:
        connection.close()
