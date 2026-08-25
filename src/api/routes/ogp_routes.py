"""OGP メタデータ取得ルート (server.py から移設)"""

from __future__ import annotations

import asyncio
import concurrent.futures
import http.client
import ipaddress
import logging
import socket
import ssl
import threading
import time
from typing import TYPE_CHECKING
from urllib.parse import SplitResult, urlencode, urljoin, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response

import certifi
import httpx

from ..router_helpers import cookie_auth_dependency

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


# OGP is a metadata endpoint, so it does not need the much larger limits used
# by document ingestion.  The socket-level read below always asks for one byte
# more than this value and fails closed when that byte is present.
OGP_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
OGP_TIMEOUT_SECONDS = 5.0
OGP_MAX_REDIRECTS = 8
_OGP_READ_CHUNK_BYTES = 64 * 1024
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_OGP_DNS_MAX_WORKERS = 4
_OGP_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_OGP_DNS_MAX_WORKERS,
    thread_name_prefix="ogp-dns",
)
_OGP_DNS_SLOTS = threading.BoundedSemaphore(_OGP_DNS_MAX_WORKERS)
_OGP_MEDIA_CONTENT_TYPES = frozenset(
    {
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


class OGPFetchError(ValueError):
    """Expected, safe-to-report failures while fetching an OGP page."""


class OGPTimeoutError(OGPFetchError):
    """The DNS lookup or pinned request exceeded the OGP timeout."""


def _is_public_ipv4(address: ipaddress.IPv4Address) -> bool:
    """Return whether an IPv4 address is safe for an outbound OGP fetch."""

    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
    )


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Return an IPv4 address encoded by an IPv6 transition mechanism.

    ``ipaddress`` exposes mapped and 6to4 addresses, but not the historical
    IPv4-compatible or NAT64 forms.  Those forms must be checked as well: a
    private low 32-bit value otherwise appears globally routable on some
    Python versions (for example ``::127.0.0.1`` and
    ``64:ff9b::7f00:1``).
    """

    mapped = address.ipv4_mapped
    if mapped is not None:
        return mapped

    six_to_four = address.sixtofour
    if six_to_four is not None:
        return six_to_four

    if address in _NAT64_WELL_KNOWN_PREFIX or int(address) >> 32 == 0:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def _is_public_ip(value: str) -> bool:
    """Return whether *value* is a globally routable address.

    ``is_global`` excludes private, loopback, link-local, multicast, reserved,
    and unspecified ranges.  The explicit checks document the SSRF boundary
    and protect against Python-version differences in ``is_global``.
    """

    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return _is_public_ipv4(address)

    embedded = _embedded_ipv4(address)
    if embedded is not None:
        # Do not over-reject a public IPv4 embedded in an IPv6 transition
        # address, but never let a private/loopback/link-local value through.
        return _is_public_ipv4(embedded)

    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
    )


def _deadline_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OGPTimeoutError("取得がタイムアウトしました")
    return remaining


def _response_socket(raw_response) -> object | None:
    """Return the underlying socket used by ``http.client.HTTPResponse``."""

    fp = getattr(raw_response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None:
        return sock
    return getattr(raw_response, "sock", None) or getattr(raw_response, "_sock", None)


def _set_response_socket_timeout(raw_response, timeout: float) -> None:
    sock = _response_socket(raw_response)
    setter = getattr(sock, "settimeout", None)
    if callable(setter):
        setter(timeout)


def _read_bounded_ogp_body(raw_response, deadline: float) -> bytes:
    """Read a response body in bounded chunks under one absolute deadline.

    ``HTTPResponse.read(n)`` only guarantees an inactivity timeout.  A peer
    that drips one byte just before each timeout can therefore keep a request
    alive forever.  Recalculate the remaining absolute deadline before every
    chunk and update the underlying socket timeout so that each read cannot
    extend the overall request budget.
    """

    body = bytearray()
    while True:
        remaining = _deadline_remaining(deadline)
        _set_response_socket_timeout(raw_response, remaining)
        read_size = min(_OGP_READ_CHUNK_BYTES, OGP_MAX_RESPONSE_BYTES + 1 - len(body))
        # ``read(n)`` may internally issue many recv calls until n bytes or
        # EOF.  ``read1`` returns after at most one underlying read, allowing
        # us to re-check the absolute deadline for every network operation.
        reader = getattr(raw_response, "read1", None)
        if not callable(reader):
            reader = raw_response.read
        try:
            chunk = reader(read_size)
        except (socket.timeout, TimeoutError) as exc:
            raise OGPTimeoutError("取得がタイムアウトしました") from exc
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > OGP_MAX_RESPONSE_BYTES:
            raise OGPFetchError("取得本文がサイズ上限を超えました")


def _looks_like_legacy_ipv4_literal(value: str) -> bool:
    """Detect browser-compatible integer/octal/hex IPv4 spellings."""

    if value.isdigit():
        return True
    if not value or not all(char in "0123456789abcdefx." for char in value):
        return False
    if "." not in value and "x" not in value:
        return False
    try:
        socket.inet_aton(value)
    except OSError:
        return False
    return True


def _consume_ogp_dns_future(future: asyncio.Future) -> None:
    """Retrieve late DNS failures after a caller timed out or was cancelled."""

    if future.cancelled():
        return
    try:
        future.exception()
    except BaseException:
        # ``exception()`` can raise CancelledError when an event loop is
        # shutting down; there is nothing useful to report in that case.
        return


def _release_ogp_dns_slot(future: concurrent.futures.Future) -> None:
    """Release a slot only when the underlying resolver really completed."""

    try:
        # Mark the exception as observed for consistency across executor
        # implementations.  The wrapped asyncio future also consumes it.
        future.exception()
    except BaseException:
        pass
    finally:
        _OGP_DNS_SLOTS.release()


async def _bounded_ogp_dns_lookup(host: str, port: int):
    """Resolve DNS in a fixed pool without queuing unbounded attacker work."""

    if not _OGP_DNS_SLOTS.acquire(blocking=False):
        raise OGPFetchError("DNS解決が混雑しているため利用できません")

    try:
        future = _OGP_DNS_EXECUTOR.submit(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except Exception as exc:
        _OGP_DNS_SLOTS.release()
        raise OGPFetchError("DNS解決を開始できません") from exc

    future.add_done_callback(_release_ogp_dns_slot)
    wrapped = asyncio.wrap_future(future)
    # If wait_for times out, shield leaves the resolver running until the
    # callback above releases the slot.  Consume a late exception to avoid an
    # ``asyncio.Future exception was never retrieved`` warning.
    wrapped.add_done_callback(_consume_ogp_dns_future)
    try:
        return await asyncio.wait_for(
            asyncio.shield(wrapped),
            timeout=OGP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise OGPTimeoutError("DNS解決がタイムアウトしました") from exc
    except (socket.gaierror, OSError) as exc:
        raise OGPFetchError("ホスト名を解決できません") from exc
    except Exception as exc:
        raise OGPFetchError("ホスト名を解決できません") from exc


def _parse_ogp_url(value: str) -> tuple[SplitResult, str, int]:
    """Validate URL syntax before any network operation.

    The returned host is normalized for DNS/Host-header handling while the
    ``SplitResult`` keeps the original path and query intact.
    """

    raw = str(value or "").strip()
    if not raw or any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise OGPFetchError("無効なURLです")
    try:
        parts = urlsplit(raw)
        scheme = parts.scheme.casefold()
        # Accessing port/hostname forces urllib to reject malformed IPv6 and
        # invalid port values instead of letting http.client parse them later.
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise OGPFetchError("無効なURLです") from exc
    if scheme not in {"http", "https"} or not host:
        raise OGPFetchError("http/httpsのURLのみ利用できます")
    if parts.username is not None or parts.password is not None:
        raise OGPFetchError("URLの認証情報は利用できません")
    normalized_host = host.rstrip(".").casefold()
    if not normalized_host or normalized_host in {
        "localhost",
        "localhost.localdomain",
    } or normalized_host.endswith(".local") or normalized_host.endswith(".localhost"):
        raise OGPFetchError("ローカルアドレスは利用できません")
    # Browsers accept legacy integer/octal/hex IPv4 spellings and canonicalize
    # them before opening a socket.  Reject those spellings instead of
    # treating a value such as ``2130706433`` as an ordinary DNS hostname.
    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        if _looks_like_legacy_ipv4_literal(normalized_host):
            raise OGPFetchError("無効なIPアドレス表記です")
    if port is not None and not 1 <= port <= 65535:
        raise OGPFetchError("無効なポートです")
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if scheme != parts.scheme:
        parts = parts._replace(scheme=scheme)
    return parts, normalized_host, effective_port


def _redact_url_for_log(value: object) -> str:
    """Keep URL diagnostics useful without logging query or credentials."""

    try:
        parts = urlsplit(str(value or ""))
        host = parts.hostname
        if not host or parts.scheme.casefold() not in {"http", "https"}:
            return "<invalid-url>"
        host_text = f"[{host}]" if ":" in host else host
        try:
            port = parts.port
        except ValueError:
            port = None
        port_text = f":{port}" if port is not None else ""
        path = parts.path or "/"
        return f"{parts.scheme.casefold()}://{host_text}{port_text}{path}"
    except (TypeError, ValueError):
        return "<invalid-url>"


def _sanitize_metadata_url(value: object, base_url: str) -> str | None:
    """Normalize an OGP image/icon URL before exposing it to a browser.

    Relative values are resolved against the final fetched page URL.  Only
    HTTP(S) URLs without credentials and without a private/reserved literal
    address are returned; malformed, ``data:``, ``javascript:``, and local
    targets are represented as ``None`` instead of being copied to ``img.src``.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        candidate = urljoin(base_url, value.strip())
        parts, host, _ = _parse_ogp_url(candidate)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not _is_public_ip(host):
                return None
        return parts.geturl()
    except (OGPFetchError, ValueError):
        return None


async def _resolve_public_ogp_endpoint(url: str) -> tuple[SplitResult, str]:
    """Resolve and pin one public address for an OGP request.

    Every call performs a fresh DNS lookup.  Callers invoke this once per
    redirect, and the resulting address is passed to the socket connector so
    a DNS rebinding cannot redirect the request into a private network.
    """

    parts, host, port = _parse_ogp_url(url)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(host):
            raise OGPFetchError("プライベートまたは予約済みアドレスは利用できません")
        return parts, host

    try:
        addresses = await _bounded_ogp_dns_lookup(host, port)
    except (OGPTimeoutError, OGPFetchError):
        raise

    resolved: list[str] = []
    for item in addresses:
        try:
            address = str(item[4][0]).split("%", 1)[0]
        except (IndexError, TypeError):
            raise OGPFetchError("ホスト名を解決できません")
        # Fail closed if DNS returns even one private/loopback/link-local
        # address.  Selecting a public address from a mixed answer would leave
        # room for resolver rebinding and platform-dependent selection.
        if not _is_public_ip(address):
            raise OGPFetchError("プライベートまたは予約済みアドレスは利用できません")
        if address not in resolved:
            resolved.append(address)
    if not resolved:
        raise OGPFetchError("公開IPアドレスを解決できません")
    return parts, resolved[0]


def _pinned_ogp_request(
    parts: SplitResult,
    address: str,
) -> httpx.Response:
    """Perform a bounded GET while connecting to the already-resolved IP."""

    deadline = time.monotonic() + OGP_TIMEOUT_SECONDS
    host = str(parts.hostname or "")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    timeout = OGP_TIMEOUT_SECONDS

    watchdog_state: dict[str, object | None] = {
        "connection": None,
        "sock": None,
        "done": False,
        "expired": False,
    }

    def abort_blocking_io() -> None:
        """Interrupt a socket read if the worker reaches its deadline."""

        if watchdog_state.get("done"):
            return
        watchdog_state["expired"] = True
        connection = watchdog_state.get("connection")
        sock = watchdog_state.get("sock")
        candidates = [sock, getattr(connection, "sock", None)]
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                candidate.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
            try:
                candidate.close()
            except (OSError, AttributeError):
                pass

    watchdog = threading.Timer(
        max(0.0, deadline - time.monotonic()),
        abort_blocking_io,
    )
    watchdog.daemon = True

    if parts.scheme == "https":

        class PinnedHTTPSConnection(http.client.HTTPSConnection):
            def connect(self) -> None:
                sock = socket.create_connection(
                    (address, port), timeout=_deadline_remaining(deadline)
                )
                watchdog_state["sock"] = sock
                self.sock = self._context.wrap_socket(sock, server_hostname=host)
                watchdog_state["sock"] = self.sock
                _set_response_socket_timeout(self, _deadline_remaining(deadline))

        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
        connection: http.client.HTTPConnection = PinnedHTTPSConnection(
            host,
            port=port,
            timeout=timeout,
            context=context,
        )
    else:

        class PinnedHTTPConnection(http.client.HTTPConnection):
            def connect(self) -> None:
                self.sock = socket.create_connection(
                    (address, port), timeout=_deadline_remaining(deadline)
                )
                watchdog_state["sock"] = self.sock
                _set_response_socket_timeout(self, _deadline_remaining(deadline))

        connection = PinnedHTTPConnection(address, port=port, timeout=timeout)

    try:
        watchdog_state["connection"] = connection
        watchdog.start()
        connection.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        host_header = host
        if ":" in host and not host.startswith("["):
            host_header = f"[{host}]"
        if parts.port is not None:
            host_header = f"{host_header}:{parts.port}"
        connection.putheader("Host", host_header)
        connection.putheader("User-Agent", "AoiTalk/1.0 OGP Fetcher")
        connection.putheader("Accept", "text/html,application/xhtml+xml")
        connection.putheader("Accept-Encoding", "identity")
        connection.putheader("Connection", "close")
        connection.endheaders()
        _set_response_socket_timeout(connection, _deadline_remaining(deadline))
        raw = connection.getresponse()
        content = _read_bounded_ogp_body(raw, deadline)
        _deadline_remaining(deadline)
        request_url = parts.geturl()
        return httpx.Response(
            raw.status,
            headers=dict(raw.getheaders()),
            content=content,
            request=httpx.Request("GET", request_url),
        )
    except socket.timeout as exc:
        raise OGPTimeoutError("取得がタイムアウトしました") from exc
    except TimeoutError as exc:
        raise OGPTimeoutError("取得がタイムアウトしました") from exc
    except Exception as exc:
        if watchdog_state.get("expired") or time.monotonic() >= deadline:
            raise OGPTimeoutError("取得がタイムアウトしました") from exc
        raise
    finally:
        watchdog_state["done"] = True
        watchdog.cancel()
        connection.close()


async def _safe_ogp_get(url: str) -> httpx.Response:
    """GET a URL with DNS pinning and validation on every redirect."""

    current = str(url)
    for redirect_count in range(OGP_MAX_REDIRECTS + 1):
        parts, address = await _resolve_public_ogp_endpoint(current)
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(_pinned_ogp_request, parts, address),
                timeout=OGP_TIMEOUT_SECONDS + 0.25,
            )
        except asyncio.TimeoutError as exc:
            raise OGPTimeoutError("取得がタイムアウトしました") from exc
        if not response.is_redirect:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        if redirect_count >= OGP_MAX_REDIRECTS:
            raise OGPFetchError("リダイレクト回数が上限を超えました")
        current = urljoin(current, location)
    raise OGPFetchError("リダイレクト回数が上限を超えました")


def register_ogp_routes(app: FastAPI, server: "WebChatServer") -> None:
    """OGP メタデータ取得ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    @app.get("/api/ogp/media")
    async def ogp_media(url: str = Query(...), _: None = Depends(require_auth)):
        """Proxy a validated OGP image through the bounded server fetcher."""

        try:
            response = await _safe_ogp_get(url)
            if not 200 <= response.status_code < 300:
                return JSONResponse(
                    {"success": False, "error": "画像を取得できません"},
                    status_code=502,
                )
            content_type = response.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().casefold()
            if media_type not in _OGP_MEDIA_CONTENT_TYPES:
                return JSONResponse(
                    {"success": False, "error": "画像形式を利用できません"},
                    status_code=415,
                )
            return Response(
                content=response.content,
                media_type=media_type,
                headers={
                    "Cache-Control": "private, max-age=300",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "default-src 'none'",
                },
            )
        except OGPTimeoutError:
            return JSONResponse(
                {"success": False, "error": "タイムアウト"},
                status_code=504,
            )
        except OGPFetchError:
            return JSONResponse(
                {"success": False, "error": "URLを取得できません"},
                status_code=400,
            )
        except Exception as exc:
            logger.warning(
                "OGP media fetch failed for %s (%s)",
                _redact_url_for_log(url),
                type(exc).__name__,
            )
            return JSONResponse(
                {"success": False, "error": "画像を取得できません"},
                status_code=502,
            )

    # ── OGP Metadata API ───────────────────────────────────────────────

    @app.get("/api/ogp")
    async def ogp_fetch(url: str = Query(...), _: None = Depends(require_auth)):
        """Fetch OGP metadata from a URL"""
        import re

        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise HTTPException(
                status_code=503, detail="beautifulsoup4 is not installed"
            )

        def is_x_status_url(value: str) -> bool:
            try:
                parsed = urlsplit(value)
            except ValueError:
                return False
            if parsed.username is not None or parsed.password is not None:
                return False
            host = (parsed.hostname or "").casefold().rstrip(".")
            if host not in {
                "x.com",
                "www.x.com",
                "twitter.com",
                "www.twitter.com",
                "mobile.twitter.com",
            }:
                return False
            return re.match(r"^/[^/]+/status(?:es)?/\d+", parsed.path) is not None

        try:
            # Validate the requested URL before contacting the X oEmbed service
            # (the service would otherwise receive an attacker-controlled URL).
            await _resolve_public_ogp_endpoint(url)

            if is_x_status_url(url):
                try:
                    oembed_url = "https://publish.twitter.com/oembed?" + urlencode(
                        {"url": url, "omit_script": "true", "dnt": "true"}
                    )
                    oembed_resp = await _safe_ogp_get(oembed_url)
                    oembed_resp.raise_for_status()
                    oembed = oembed_resp.json()
                    embed_html = oembed.get("html")
                    if embed_html:
                        author = oembed.get("author_name")
                        title = f"{author} on X" if author else "X post"
                        return JSONResponse(
                            {
                                "success": True,
                                "title": title,
                                "description": None,
                                "image": None,
                                "url": url,
                                "favicon": "https://abs.twimg.com/favicons/twitter.3.ico",
                                "embed_type": "x-post",
                                "embed_html": embed_html,
                                "provider_name": oembed.get("provider_name")
                                or "Twitter",
                            }
                        )
                except Exception as exc:
                    logger.warning(
                        "X oEmbed fetch failed for %s (%s)",
                        _redact_url_for_log(url),
                        type(exc).__name__,
                    )

            resp = await _safe_ogp_get(url)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            def og(prop: str):
                tag = soup.find("meta", property=f"og:{prop}")
                return tag["content"] if tag and tag.get("content") else None

            title = og("title") or (soup.title.string if soup.title else None)
            description = og("description")
            if not description:
                meta_desc = soup.find("meta", attrs={"name": "description"})
                description = (
                    meta_desc["content"]
                    if meta_desc and meta_desc.get("content")
                    else None
                )
            try:
                response_url = str(resp.url)
            except (AttributeError, RuntimeError):
                response_url = url
            image = _sanitize_metadata_url(og("image"), response_url)

            # favicon
            favicon = None
            icon_link = soup.find(
                "link",
                rel=lambda v: v and "icon" in (v if isinstance(v, list) else [v]),
            )
            if icon_link and icon_link.get("href"):
                favicon = _sanitize_metadata_url(
                    icon_link["href"],
                    response_url,
                )

            return JSONResponse(
                {
                    "success": True,
                    "title": title,
                    "description": description,
                    "image": image,
                    "url": url,
                    "favicon": favicon,
                }
            )
        except OGPTimeoutError:
            return JSONResponse(
                {"success": False, "error": "タイムアウト", "url": url}
            )
        except OGPFetchError:
            return JSONResponse(
                {"success": False, "error": "URLを取得できません", "url": url}
            )
        except httpx.HTTPStatusError:
            return JSONResponse(
                {"success": False, "error": "取得先がエラーを返しました", "url": url}
            )
        except Exception as exc:
            # Do not include exception text or traceback here: httpx errors can
            # echo the attacker-controlled query/credentials from the URL.
            logger.error(
                "OGP metadata fetch failed for %s (%s)",
                _redact_url_for_log(url),
                type(exc).__name__,
            )
            return JSONResponse(
                {"success": False, "error": "OGPメタデータを取得できません", "url": url}
            )
