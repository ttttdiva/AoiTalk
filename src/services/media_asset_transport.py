"""Public HTTPS asset reads with DNS validation at the TCP boundary.

Only the resolved public numeric address reaches the network backend. The
original origin remains in HTTPCore, preserving Host, SNI and certificate
hostname verification. No proxy, redirect, custom SNI or caller credential is
accepted. This is independent of the authenticated provider-write client.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from typing import Any

import httpcore
import httpx


class PublicAssetDestinationError(httpx.RequestError):
    """A bounded, secret-free destination rejection."""


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    # Reject transition encodings as well as local/private/link-local IPs.
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None:
            return False
    return address.is_global and not address.is_multicast


class PublicAssetNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, *, backend: Any = None):
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout=None, local_address=None, socket_options=None):
        if port != 443 or local_address is not None or socket_options:
            raise PublicAssetDestinationError("media asset connection policy rejected")
        host = host.rstrip(".").lower()
        if not host or "%" in host or host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise PublicAssetDestinationError("media asset destination is not public")
        started = time.monotonic()
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            try:
                lookup = asyncio.to_thread(socket.getaddrinfo, host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
                rows = await asyncio.wait_for(lookup, timeout=timeout) if timeout is not None else await lookup
            except (OSError, TimeoutError):
                raise PublicAssetDestinationError("media asset host could not be resolved") from None
            addresses = list(dict.fromkeys(row[4][0] for row in rows if row[0] in {socket.AF_INET, socket.AF_INET6}))
        else:
            addresses = [str(literal)]
        if not addresses or any(not _public_address(address) for address in addresses):
            raise PublicAssetDestinationError("media asset destination is not public")
        # All DNS answers are checked once, then only numeric addresses are
        # tried. A failed IPv6 connection cannot cause a hostname re-resolution.
        for index, address in enumerate(addresses):
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("media asset connection timed out")
            try:
                return await self._backend.connect_tcp(address, port, timeout=remaining)
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                if index == len(addresses) - 1:
                    raise
        raise PublicAssetDestinationError("media asset destination is not public")

    async def connect_unix_socket(self, *args, **kwargs):
        raise PublicAssetDestinationError("media asset socket policy rejected")

    async def sleep(self, seconds: float):
        await asyncio.sleep(seconds)


_CORE_ERRORS = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
)


def _translate(exc: Exception):
    for core_error, client_error in _CORE_ERRORS:
        if isinstance(exc, core_error):
            raise client_error("media asset transport failed") from None
    raise exc


class _AssetResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self._stream = stream

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                yield chunk
        except Exception as exc:
            _translate(exc)

    async def aclose(self):
        await self._stream.aclose()


class PublicAssetTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, network_backend=None):
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=network_backend or PublicAssetNetworkBackend(),
            ssl_context=httpcore.default_ssl_context(),
            max_connections=4, max_keepalive_connections=0, retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if (request.method != "GET" or url.scheme != "https" or url.port not in {None, 443}
                or url.username or url.password or url.fragment or not url.host
                or any(key in request.headers for key in ("Authorization", "Cookie", "Proxy-Authorization"))
                or "sni_hostname" in request.extensions):
            raise PublicAssetDestinationError("media asset request policy rejected")
        # Caller overrides must never redirect Host independently of TLS.
        if request.headers.get("Host") != url.netloc.decode("ascii"):
            raise PublicAssetDestinationError("media asset origin policy rejected")
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=url.raw_scheme, host=url.raw_host, port=url.port, target=url.raw_path),
            headers=request.headers.raw, content=request.stream,
            extensions={"timeout": request.extensions.get("timeout", {})},
        )
        try:
            response = await self._pool.handle_async_request(core_request)
        except Exception as exc:
            _translate(exc)
        return httpx.Response(response.status, headers=response.headers,
            stream=_AssetResponseStream(response.stream), extensions=response.extensions)

    async def aclose(self):
        await self._pool.aclose()


def public_asset_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=PublicAssetTransport(), trust_env=False,
        follow_redirects=False, timeout=httpx.Timeout(180, connect=15),
        headers={"User-Agent": "AoiTalk-MediaOps/3"})
