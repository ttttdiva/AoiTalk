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
import json
import multiprocessing
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

# 接続テスト・読み取りのデフォルトタイムアウト（秒）。
_DEFAULT_TIMEOUT = 10.0
# capabilities キャッシュの存続時間（秒）。短TTLで仕様乖離の追従性を保つ。
_CAPABILITIES_TTL = 60.0
# JSON API とファイル中継の最大応答サイズ。ストリーム中に強制する。
_MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_RAW_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_REDIRECTS = 5
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_REMOTE_DNS_MAX_WORKERS = 4
_REMOTE_DNS_SLOTS = threading.BoundedSemaphore(_REMOTE_DNS_MAX_WORKERS)
_REMOTE_DNS_RESULT_MAX_BYTES = 16 * 1024
_REMOTE_DNS_MAX_ADDRESSES = 64
_REMOTE_DNS_POLL_SECONDS = 0.01
_REMOTE_DNS_STOP_SECONDS = 0.25
_REMOTE_DNS_CONTEXT = multiprocessing.get_context("spawn")
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class RemoteConnectorError(RuntimeError):
    """リモート接続・応答に関する失敗。"""


@dataclass(frozen=True)
class RemoteRawResponse:
    """ファイル中継用のレスポンススナップショット。"""

    status_code: int
    headers: Dict[str, str]
    content: bytes


@dataclass(frozen=True)
class _PinnedTarget:
    """DNS検証済みの論理URLと実際の接続先。"""

    logical_url: httpx.URL
    connect_url: httpx.URL
    host_header: str
    sni_hostname: str


@dataclass(frozen=True)
class _BufferedResponse:
    status_code: int
    headers: Dict[str, str]
    content: bytes


class RemoteRawStream:
    """ライフサイクルを明示的に管理する、デコード済みリモート応答。

    ``httpx.AsyncClient`` と ``httpx.Response`` は、``StreamingResponse`` の
    body iterator が消費し終わるまで保持する必要がある。呼び出し側は
    ``aiter_bytes`` の利用を ``try/finally`` で囲み、必ず ``aclose`` を呼ぶ。
    ``aclose`` は冪等で、切断やキャンセル時にも上流のリソースを閉じる。
    """

    def __init__(
        self,
        *,
        response: httpx.Response,
        client: httpx.AsyncClient,
        status_code: int,
        headers: Dict[str, str],
        max_bytes: Optional[int],
        read_timeout: float,
        request_url: str,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._response = response
        self._client = client
        self._max_bytes = max_bytes
        self._read_timeout = read_timeout
        self._request_url = request_url
        self._bytes_read = 0
        self._iterated = False
        self._closed = False
        self._close_task: Optional[asyncio.Task[None]] = None

    async def __aenter__(self) -> "RemoteRawStream":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """上流 response/client を閉じる（複数回呼び出し可能）。"""
        if self._close_task is None:
            self._closed = True

            async def close_resources() -> None:
                try:
                    await self._response.aclose()
                finally:
                    await self._client.aclose()

            self._close_task = asyncio.create_task(close_resources())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            # クライアント切断で iterator がキャンセルされても、cleanup の
            # task 自体は完了させてから cancellation を呼び出し側へ返す。
            await asyncio.shield(self._close_task)
            raise

    async def aiter_bytes(self):
        """上流のデコード済みバイト列を逐次返す。

        body 全量を保持せず、デコード後の累積サイズを chunk 単位で検査する。
        timeout は body の各 read に適用するアイドルタイムアウトとして扱う。
        """
        if self._iterated:
            raise RuntimeError("remote response stream has already been consumed")
        if self._closed:
            raise RuntimeError("remote response stream is closed")
        self._iterated = True
        completed = False
        try:
            # ``asyncio.timeout`` is only available from Python 3.11.  Waiting
            # for each ``__anext__`` keeps compatibility with Python 3.10 too.
            iterator = self._response.aiter_bytes().__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        iterator.__anext__(), timeout=self._read_timeout
                    )
                except StopAsyncIteration:
                    break
                self._bytes_read += len(chunk)
                if (
                    self._max_bytes is not None
                    and self._bytes_read > self._max_bytes
                ):
                    raise RemoteConnectorError(
                        "remote response exceeded the size limit"
                    )
                yield chunk
            completed = True
        except asyncio.TimeoutError as exc:
            raise RemoteConnectorError(
                f"request to {self._request_url} timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise RemoteConnectorError(
                f"request to {self._request_url} failed: {exc}"
            ) from exc
        finally:
            # Successful completion intentionally leaves the resources open until
            # caller's ``finally: await stream.aclose()``.  Every exceptional or
            # cancelled path closes eagerly to avoid leaking sockets.
            if not completed:
                await self.aclose()


def _embedded_ipv4(
    address: ipaddress.IPv6Address,
) -> Optional[ipaddress.IPv4Address]:
    mapped = address.ipv4_mapped
    if mapped is not None:
        return mapped
    six_to_four = address.sixtofour
    if six_to_four is not None:
        return six_to_four
    if address in _NAT64_WELL_KNOWN_PREFIX or int(address) >> 32 == 0:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


def _is_disallowed_remote_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None:
            return _is_disallowed_remote_address(embedded)
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or not address.is_global
    )


def _dns_process_worker(connection, host: str, port: int) -> None:
    """Resolve one hostname and return a strictly bounded JSON message."""
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses: list[str] = []
        for result in results:
            address = str(result[4][0])
            if len(address) > 64:
                raise ValueError("invalid DNS address")
            if address not in addresses:
                addresses.append(address)
            if len(addresses) >= _REMOTE_DNS_MAX_ADDRESSES:
                break
        payload = json.dumps(
            {"ok": True, "addresses": addresses},
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > _REMOTE_DNS_RESULT_MAX_BYTES:
            raise ValueError("DNS result too large")
    except BaseException:
        payload = b'{"ok":false}'
    try:
        connection.send_bytes(payload)
    finally:
        connection.close()


def _spawn_dns_process(host: str, port: int):
    parent_connection, child_connection = _REMOTE_DNS_CONTEXT.Pipe(duplex=False)
    process = _REMOTE_DNS_CONTEXT.Process(
        target=_dns_process_worker,
        args=(child_connection, host, port),
        name="remote-connector-dns",
        daemon=True,
    )
    try:
        process.start()
    except BaseException:
        parent_connection.close()
        child_connection.close()
        raise
    child_connection.close()
    return process, parent_connection


def _stop_dns_process(process) -> bool:
    """Synchronously stop a DNS child from a bootstrap/cleanup thread."""
    try:
        if process.is_alive():
            process.terminate()
            process.join(_REMOTE_DNS_STOP_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(_REMOTE_DNS_STOP_SECONDS)
        if process.is_alive():
            return False
        close = getattr(process, "close", None)
        if close is not None:
            close()
        return True
    except (AssertionError, OSError, ValueError):
        try:
            return not process.is_alive()
        except (AssertionError, OSError, ValueError):
            return False


def _cleanup_dns_resources(process, connection) -> None:
    """Close one DNS job and restore capacity only after its child is dead."""
    try:
        connection.close()
    except (OSError, ValueError):
        pass
    if _stop_dns_process(process):
        _REMOTE_DNS_SLOTS.release()


class _DNSBootstrap:
    """Daemon-thread owner for a potentially blocking Windows spawn."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._abandoned = False
        self._owns_cleanup = False
        self._result = None
        self.error: BaseException | None = None

    def abandon(self):
        """Detach pending startup or claim a result that raced with timeout."""
        with self._lock:
            self._abandoned = True
            result = self._result
            self._result = None
            detached = self._owns_cleanup or (result is None and not self.done.is_set())
            return result, detached

    def claim(self):
        with self._lock:
            if not self.done.is_set() or self.error is not None:
                return None
            result = self._result
            self._result = None
            return result

    def run(self) -> None:
        cleanup_result = None
        release_without_process = False
        try:
            result = _spawn_dns_process(self.host, self.port)
            with self._lock:
                if self._abandoned:
                    self._owns_cleanup = True
                    cleanup_result = result
                else:
                    self._result = result
                self.done.set()
        except BaseException as exc:
            with self._lock:
                self.error = exc
                release_without_process = self._abandoned
                self._owns_cleanup = release_without_process
                # Publish failure and its cleanup ownership atomically.  A
                # concurrent abandon must see either pending bootstrap or a
                # completed error, never the gap between those states.
                self.done.set()

        if cleanup_result is not None:
            _cleanup_dns_resources(*cleanup_result)
        elif release_without_process:
            _REMOTE_DNS_SLOTS.release()


async def _bounded_dns_lookup(host: str, port: int, timeout: float):
    """Resolve in a killable process without queuing attacker-controlled work."""
    if not _REMOTE_DNS_SLOTS.acquire(blocking=False):
        raise RemoteConnectorError("remote DNS resolver is busy")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    bootstrap = _DNSBootstrap(host, port)
    bootstrap_thread = threading.Thread(
        target=bootstrap.run,
        name="remote-dns-bootstrap",
        daemon=True,
    )
    try:
        bootstrap_thread.start()
    except RuntimeError as exc:
        _REMOTE_DNS_SLOTS.release()
        raise RemoteConnectorError("remote DNS resolution could not start") from exc
    process = None
    connection = None
    bootstrap_detached = False

    try:
        while not bootstrap.done.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                result, bootstrap_detached = bootstrap.abandon()
                if result is not None:
                    process, connection = result
                raise RemoteConnectorError("remote DNS resolution timed out")
            await asyncio.sleep(min(_REMOTE_DNS_POLL_SECONDS, remaining))
        if bootstrap.error is not None:
            raise RemoteConnectorError(
                "remote DNS resolution could not start"
            ) from bootstrap.error
        result = bootstrap.claim()
        if result is None:
            raise RemoteConnectorError("remote DNS resolution could not start")
        process, connection = result

        while not connection.poll():
            if not process.is_alive():
                raise RemoteConnectorError("remote host cannot be resolved safely")
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RemoteConnectorError("remote DNS resolution timed out")
            await asyncio.sleep(min(_REMOTE_DNS_POLL_SECONDS, remaining))
        try:
            payload = connection.recv_bytes(_REMOTE_DNS_RESULT_MAX_BYTES)
            result = json.loads(payload)
        except (EOFError, OSError, UnicodeDecodeError, ValueError) as exc:
            raise RemoteConnectorError(
                "remote host cannot be resolved safely"
            ) from exc
        addresses = result.get("addresses") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or not isinstance(addresses, list)
            or not addresses
            or len(addresses) > _REMOTE_DNS_MAX_ADDRESSES
            or not all(
                isinstance(address, str) and len(address) <= 64
                for address in addresses
            )
        ):
            raise RemoteConnectorError("remote host cannot be resolved safely")
        return addresses
    finally:
        if process is None:
            result, bootstrap_detached = bootstrap.abandon()
            if result is not None:
                process, connection = result
        if process is not None:
            # One off-loop operation owns both cleanup and slot release.  If
            # the caller is cancelled, shield lets that ownership complete.
            cleanup = asyncio.create_task(
                asyncio.to_thread(_cleanup_dns_resources, process, connection)
            )
            await asyncio.shield(cleanup)
        elif not bootstrap_detached:
            _REMOTE_DNS_SLOTS.release()


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

    @staticmethod
    def _origin(url: httpx.URL) -> tuple[str, str, int]:
        scheme = url.scheme.lower()
        default_port = 443 if scheme == "https" else 80
        return scheme, url.host.lower().rstrip("."), url.port or default_port

    @staticmethod
    def _host_header(hostname: str, port: int, scheme: str) -> str:
        try:
            is_ipv6 = ipaddress.ip_address(hostname).version == 6
        except ValueError:
            is_ipv6 = False
        host = f"[{hostname}]" if is_ipv6 else hostname
        default_port = 443 if scheme == "https" else 80
        return host if port == default_port else f"{host}:{port}"

    async def _pin_safe_network_target(self, url: str) -> _PinnedTarget:
        """Resolve once, validate every answer, and return an IP-pinned URL."""
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            if parsed.scheme not in {"http", "https"} or not hostname:
                raise RemoteConnectorError("remote base URL must be an HTTP(S) URL")
            if parsed.username or parsed.password:
                raise RemoteConnectorError("remote base URL must not contain credentials")
            lowered = hostname.lower().rstrip(".")
            if (
                not self._private_targets_allowed()
                and (
                    lowered in {"localhost", "localhost.localdomain"}
                    or lowered.endswith(".localhost")
                )
            ):
                raise RemoteConnectorError("private remote server hosts are disabled")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            try:
                literal_address = ipaddress.ip_address(lowered)
            except ValueError:
                addresses = await _bounded_dns_lookup(
                    hostname,
                    port,
                    self._timeout,
                )
                resolved_addresses = addresses
            else:
                resolved_addresses = [str(literal_address)]
        except RemoteConnectorError:
            raise
        except (ValueError, socket.gaierror, OSError, httpx.InvalidURL) as exc:
            raise RemoteConnectorError(f"remote host cannot be resolved safely: {exc}") from exc

        if not resolved_addresses:
            raise RemoteConnectorError("remote host did not resolve to an address")

        pinned_address: Optional[ipaddress.IPv4Address | ipaddress.IPv6Address] = None
        for resolved_address in resolved_addresses:
            try:
                address = ipaddress.ip_address(resolved_address)
            except ValueError as exc:
                raise RemoteConnectorError("remote host resolved to an invalid address") from exc
            if not self._private_targets_allowed() and _is_disallowed_remote_address(
                address
            ):
                raise RemoteConnectorError("private remote server hosts are disabled")
            if pinned_address is None:
                pinned_address = address

        assert pinned_address is not None
        try:
            logical_url = httpx.URL(url)
            connect_url = logical_url.copy_with(host=str(pinned_address))
        except (ValueError, httpx.InvalidURL) as exc:
            raise RemoteConnectorError(f"remote URL is invalid: {exc}") from exc
        normalized_hostname = logical_url.host.lower().rstrip(".")
        return _PinnedTarget(
            logical_url=logical_url,
            connect_url=connect_url,
            host_header=self._host_header(
                normalized_hostname,
                logical_url.port or port,
                logical_url.scheme,
            ),
            sni_hostname=normalized_hostname,
        )

    async def _ensure_safe_network_target(self) -> None:
        """Prevent a saved hostname from being used for SSRF into private networks."""
        await self._pin_safe_network_target(self._base_url)

    def _headers(self, *, include_authorization: bool = True) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if include_authorization and self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    @staticmethod
    async def _read_bounded_response(
        response: httpx.Response,
        *,
        max_bytes: int,
    ) -> bytes:
        content = bytearray()
        # Bound decoded bytes, not wire bytes.  httpx transparently decodes
        # gzip/br content here, preventing both corrupted proxy downloads and
        # compressed responses from bypassing the memory limit.
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > max_bytes:
                raise RemoteConnectorError("remote response exceeded the size limit")
            content.extend(chunk)
        return bytes(content)

    @staticmethod
    def _stream_response_headers(response: httpx.Response) -> Dict[str, str]:
        """ストリーム用に、上流ヘッダーをデコード後の body と整合させる。"""
        headers = {
            key.lower(): value
            for key, value in response.headers.items()
            # StreamingResponse が再生成する hop-by-hop ヘッダーは転送しない。
            if key.lower() not in {"transfer-encoding", "connection"}
        }
        encoding = response.headers.get("content-encoding")
        if encoding:
            # ``aiter_bytes`` は httpx が content-encoding を復号して返すため、
            # encoded length/range は downstream の body と一致しない。
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            headers.pop("accept-ranges", None)
            headers.pop("content-range", None)
        return headers

    async def open_raw_stream(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        max_bytes: Optional[int] = None,
        request_headers: Optional[Dict[str, str]] = None,
    ) -> RemoteRawStream:
        """ファイル応答を逐次中継するためのハンドルを開く。

        ハンドルは response/client を保持したまま返される。呼び出し側は
        ``async for chunk in stream.aiter_bytes()`` を実行し、クライアント切断
        を含む全経路で ``await stream.aclose()`` を呼び出すこと。デフォルトでは
        body size に上限を設けず、必要な呼び出し側だけ ``max_bytes`` を指定する。
        """
        url = self._url(path)
        try:
            logical_url = httpx.URL(url)
            if params is not None:
                logical_url = logical_url.copy_merge_params(params)
        except (ValueError, httpx.InvalidURL) as exc:
            raise RemoteConnectorError(f"remote URL is invalid: {exc}") from exc

        try:
            base_origin = self._origin(httpx.URL(self._base_url))
        except (ValueError, httpx.InvalidURL) as exc:
            raise RemoteConnectorError(f"remote URL is invalid: {exc}") from exc

        current_method = method.upper()
        current_json = json_body
        redirect_count = 0
        safe_request_headers = {
            key.lower(): value
            for key, value in (request_headers or {}).items()
            if key.lower() in {
                "range",
                "if-range",
                "if-none-match",
                "if-modified-since",
            }
        }
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        client: Optional[httpx.AsyncClient] = None
        response: Optional[httpx.Response] = None

        async def open_once() -> RemoteRawStream:
            nonlocal logical_url, current_method, current_json, redirect_count
            nonlocal client, response
            while True:
                target = await self._pin_safe_network_target(str(logical_url))
                include_authorization = self._origin(logical_url) == base_origin
                headers = self._headers(
                    include_authorization=include_authorization
                )
                headers["Host"] = target.host_header
                headers.update(safe_request_headers)
                # A fresh client per redirect keeps DNS pinning and SNI explicit;
                # only the final client/response are retained by the handle.
                client = httpx.AsyncClient(
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                )
                try:
                    request = client.build_request(
                        current_method,
                        target.connect_url,
                        json=current_json,
                        headers=headers,
                        extensions={"sni_hostname": target.sni_hostname},
                    )
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    response = await asyncio.wait_for(
                        client.send(request, stream=True),
                        timeout=remaining,
                    )
                    location = response.headers.get("location")
                    if (
                        response.status_code in _REDIRECT_STATUS_CODES
                        and location
                    ):
                        if redirect_count >= _MAX_REDIRECTS:
                            raise RemoteConnectorError(
                                "remote returned too many redirects"
                            )
                        try:
                            redirected_url = httpx.URL(
                                urljoin(str(target.logical_url), location)
                            )
                        except (ValueError, httpx.InvalidURL) as exc:
                            raise RemoteConnectorError(
                                f"remote redirect URL is invalid: {exc}"
                            ) from exc
                        if (
                            target.logical_url.scheme == "https"
                            and redirected_url.scheme == "http"
                        ):
                            raise RemoteConnectorError(
                                "remote redirect attempted to downgrade HTTPS"
                            )
                        old_url = logical_url
                        logical_url = redirected_url
                        redirect_count += 1
                        if (
                            response.status_code in {302, 303}
                            and current_method != "HEAD"
                        ) or (
                            response.status_code == 301
                            and current_method == "POST"
                        ):
                            current_method = "GET"
                            current_json = None
                        if (
                            self._origin(old_url) != self._origin(redirected_url)
                            and current_method not in {"GET", "HEAD"}
                        ):
                            raise RemoteConnectorError(
                                "remote attempted a cross-origin write redirect"
                            )
                        await response.aclose()
                        response = None
                        await client.aclose()
                        client = None
                        continue

                    content_length = response.headers.get("content-length")
                    # Content-Length describes encoded wire bytes when an
                    # encoding is present.  The actual limit is enforced on
                    # decoded chunks below, so only identity responses can be
                    # rejected solely from this header.
                    content_encoding = response.headers.get("content-encoding")
                    if (
                        max_bytes is not None
                        and response.status_code < 400
                        and content_length
                        and not content_encoding
                    ):
                        try:
                            declared_length = int(content_length)
                        except ValueError:
                            declared_length = None
                        if declared_length is not None and (
                            declared_length < 0
                            or declared_length > max_bytes
                        ):
                            raise RemoteConnectorError(
                                "remote response exceeded the size limit"
                            )
                    response_headers = self._stream_response_headers(response)
                    return RemoteRawStream(
                        response=response,
                        client=client,
                        status_code=response.status_code,
                        headers=response_headers,
                        max_bytes=max_bytes,
                        read_timeout=self._timeout,
                        request_url=url,
                    )
                except BaseException:
                    if response is not None:
                        await response.aclose()
                        response = None
                    if client is not None:
                        await client.aclose()
                        client = None
                    raise

        try:
            return await asyncio.wait_for(open_once(), timeout=self._timeout)
        except RemoteConnectorError:
            raise
        except asyncio.TimeoutError as exc:
            raise RemoteConnectorError(f"request to {url} timed out") from exc
        except httpx.HTTPError as exc:
            raise RemoteConnectorError(f"request to {url} failed: {exc}") from exc

    async def _send_bounded(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        max_bytes: int,
    ) -> _BufferedResponse:
        try:
            logical_url = httpx.URL(url)
            if params is not None:
                logical_url = logical_url.copy_merge_params(params)
        except (ValueError, httpx.InvalidURL) as exc:
            raise RemoteConnectorError(f"remote URL is invalid: {exc}") from exc

        try:
            base_origin = self._origin(httpx.URL(self._base_url))
        except (ValueError, httpx.InvalidURL) as exc:
            raise RemoteConnectorError(f"remote URL is invalid: {exc}") from exc

        current_method = method.upper()
        current_json = json_body
        redirect_count = 0

        async def send() -> _BufferedResponse:
            nonlocal logical_url, current_method, current_json, redirect_count
            while True:
                target = await self._pin_safe_network_target(str(logical_url))
                include_authorization = self._origin(logical_url) == base_origin
                headers = self._headers(
                    include_authorization=include_authorization
                )
                headers["Host"] = target.host_header
                async with httpx.AsyncClient(
                    timeout=self._timeout,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        current_method,
                        target.connect_url,
                        json=current_json,
                        headers=headers,
                        extensions={"sni_hostname": target.sni_hostname},
                    ) as response:
                        location = response.headers.get("location")
                        if (
                            response.status_code in _REDIRECT_STATUS_CODES
                            and location
                        ):
                            if redirect_count >= _MAX_REDIRECTS:
                                raise RemoteConnectorError(
                                    "remote returned too many redirects"
                                )
                            try:
                                redirected_url = httpx.URL(
                                    urljoin(str(target.logical_url), location)
                                )
                            except (ValueError, httpx.InvalidURL) as exc:
                                raise RemoteConnectorError(
                                    f"remote redirect URL is invalid: {exc}"
                                ) from exc
                            if (
                                target.logical_url.scheme == "https"
                                and redirected_url.scheme == "http"
                            ):
                                raise RemoteConnectorError(
                                    "remote redirect attempted to downgrade HTTPS"
                                )
                            logical_url = redirected_url
                            redirect_count += 1
                            if (
                                response.status_code == 303
                                and current_method != "HEAD"
                            ) or (
                                response.status_code == 302
                                and current_method != "HEAD"
                            ) or (
                                response.status_code == 301
                                and current_method == "POST"
                            ):
                                current_method = "GET"
                                current_json = None
                            if (
                                self._origin(target.logical_url)
                                != self._origin(redirected_url)
                                and current_method not in {"GET", "HEAD"}
                            ):
                                raise RemoteConnectorError(
                                    "remote attempted a cross-origin write redirect"
                                )
                            continue

                        if response.status_code >= 400:
                            return _BufferedResponse(
                                status_code=response.status_code,
                                headers=dict(response.headers),
                                content=b"",
                            )

                        content = await self._read_bounded_response(
                            response,
                            max_bytes=max_bytes,
                        )
                        return _BufferedResponse(
                            status_code=response.status_code,
                            headers=dict(response.headers),
                            content=content,
                        )
        try:
            return await asyncio.wait_for(send(), timeout=self._timeout)
        except RemoteConnectorError:
            raise
        except asyncio.TimeoutError as exc:
            raise RemoteConnectorError(f"request to {url} timed out") from exc
        except httpx.HTTPError as exc:
            raise RemoteConnectorError(f"request to {url} failed: {exc}") from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = self._url(path)
        response = await self._send_bounded(
            method,
            url,
            params=params,
            json_body=json_body,
            max_bytes=_MAX_JSON_RESPONSE_BYTES,
        )
        if response.status_code == 401:
            raise RemoteConnectorError("remote authentication failed (401)")
        if response.status_code >= 400:
            raise RemoteConnectorError(
                f"remote returned {response.status_code} for {method} {path}"
            )
        try:
            return json.loads(response.content)
        except (UnicodeDecodeError, ValueError):
            return None

    async def request_raw(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
    ) -> RemoteRawResponse:
        """JSONに変換せず、許可済みファイル応答を中継する。"""
        url = self._url(path)
        response = await self._send_bounded(
            method,
            url,
            params=params,
            max_bytes=_MAX_RAW_RESPONSE_BYTES,
        )
        if response.status_code == 401:
            raise RemoteConnectorError("remote authentication failed (401)")
        if response.status_code >= 400:
            raise RemoteConnectorError(
                f"remote returned {response.status_code} for {method} {path}"
            )
        allowed_headers = {
            "content-type",
            "content-disposition",
            "accept-ranges",
            "content-range",
        }
        response_headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower() in allowed_headers
        }
        if response.headers.get("content-encoding"):
            # Encoded byte ranges no longer describe the decoded body.
            response_headers.pop("accept-ranges", None)
            response_headers.pop("content-range", None)
        # The buffered body is decoded, so the upstream encoded length and
        # Content-Encoding are intentionally not forwarded.
        response_headers["content-length"] = str(len(response.content))
        return RemoteRawResponse(
            status_code=response.status_code,
            headers=response_headers,
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
