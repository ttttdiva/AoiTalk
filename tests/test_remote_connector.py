import asyncio

import httpx
import pytest

from src.memory.remote_connector import (
    RemoteServerConnector,
    RemoteConnectorError,
)


def _connector_with_handler(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    connector = RemoteServerConnector(
        base_url="https://remote.example.com",
        auth_token="aoitpat_dummy",
        **kwargs,
    )
    # MockTransport を使うため _request 内のクライアント生成を差し替える。
    original_request = connector._request

    async def patched_request(method, path, *, params=None, json_body=None):
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.request(
                method,
                connector._url(path),
                params=params,
                json=json_body,
                headers=connector._headers(),
            )
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

    connector._request = patched_request  # type: ignore[assignment]
    return connector, original_request


def test_headers_include_bearer():
    connector = RemoteServerConnector(
        base_url="https://remote.example.com", auth_token="aoitpat_xyz"
    )
    headers = connector._headers()
    assert headers["Authorization"] == "Bearer aoitpat_xyz"


def test_url_join_strips_slashes():
    connector = RemoteServerConnector(
        base_url="https://remote.example.com/", auth_token=None
    )
    assert connector._url("/api/capabilities") == (
        "https://remote.example.com/api/capabilities"
    )


def test_test_connection_returns_capabilities():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer aoitpat_dummy"
        return httpx.Response(200, json={"version": "1.0", "features": {}})

    connector, _ = _connector_with_handler(handler)
    result = asyncio.run(connector.test_connection())
    assert result["version"] == "1.0"


def test_test_connection_raises_on_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Unauthorized"})

    connector, _ = _connector_with_handler(handler)
    with pytest.raises(RemoteConnectorError):
        asyncio.run(connector.test_connection())


def test_capabilities_cache_reuses_response():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"version": "1.0", "features": {}})

    connector, _ = _connector_with_handler(handler)
    asyncio.run(connector.fetch_capabilities())
    asyncio.run(connector.fetch_capabilities())
    assert calls["count"] == 1


def test_write_blocked_when_feature_disabled():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/capabilities":
            return httpx.Response(
                200, json={"features": {"tasks": False}}
            )
        return httpx.Response(200, json={"ok": True})

    connector, _ = _connector_with_handler(handler)
    with pytest.raises(RemoteConnectorError):
        asyncio.run(
            connector.write(
                "POST", "/api/tasks", required_feature="tasks"
            )
        )
