"""再利用可能なURL直接取得サービス。

検索結果の推測と、元URLから取得できた事実を混同しないため、URLごとに
直接取得結果を独立して返す。X は公開API群、GitHubとHugging Faceは
リポジトリAPIとREADMEを優先して試す。Civitaiのモデルページは公開REST APIを使う。
"""

from __future__ import annotations

import asyncio
import errno
import html
import http.client
import ipaddress
import inspect
import json
import os
import re
import select
import socket
import ssl
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from uuid import UUID
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import certifi
import httpx
from bs4 import BeautifulSoup

from .deep_research_service import DeepResearchSearchClient, DeepResearchSource
from .outbound_privacy_service import (
    EgressDescriptor,
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)
from .yahoo_realtime_search_service import canonicalize_x_url, x_status_id
from .x_cookie_service import (
    XCookieResolution,
    load_global_x_cookie,
    resolve_x_cookie,
)


_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
_GITHUB_HOSTS = {"github.com", "www.github.com"}
_HUGGING_FACE_HOSTS = {"huggingface.co", "www.huggingface.co"}
_CIVITAI_HOSTS = {
    "civitai.com",
    "www.civitai.com",
    "civitai.red",
    "www.civitai.red",
}
_CIVITAI_API_ORIGIN = "https://civitai.com"
_CIVITAI_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class _CivitaiRef:
    host: str
    model_id: int | None = None
    version_id: int | None = None
    slug: str | None = None


@dataclass
class UrlFetchResult:
    requested_url: str
    final_url: str = ""
    success: bool = False
    provider: str = "http"
    title: str = ""
    og_title: str = ""
    og_description: str = ""
    body: str = ""
    author: str = ""
    published_at: str = ""
    quoted_post: str = ""
    thread_context: list[str] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)
    media_descriptions: list[str] = field(default_factory=list)
    # JSON is kept as a parsed, bounded response for public metadata callers.
    # ``body`` remains the textual excerpt used by the historical clip path.
    json_data: Any = None
    status_code: int = 0
    response_headers: dict[str, str] = field(default_factory=dict)
    error: str = ""
    acquisition_status: str = "fetch_failed"

    def __post_init__(self) -> None:
        if self.success:
            self.acquisition_status = "success"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _ByteBudgetExceeded(ValueError):
    """Raised when one UrlIngestService instance has no wire budget left."""


class _TransportCancelled(Exception):
    """Raised inside the synchronous pinned transport after ``cancel()``."""


def _consume_transport_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        return


class _ByteBudgetReservation:
    """Thread-safe reservation held by one pinned request.

    ``_pinned_request`` runs in a worker thread because ``http.client`` is
    synchronous.  Reserving the maximum number of bytes before creating that
    thread is intentional: cancellation cannot leave an unbounded response
    reader running after the caller has stopped waiting.
    """

    def __init__(self, budget: "_ByteBudget", reserved: int) -> None:
        self._budget = budget
        self.reserved = int(reserved)
        self._settled = False
        self._lock = threading.Lock()

    def settle(self, actual: int) -> None:
        with self._lock:
            if self._settled:
                return
            self._settled = True
        self._budget.settle(self.reserved, max(0, int(actual)))

    @property
    def settled(self) -> bool:
        with self._lock:
            return self._settled


class _ByteBudget:
    """Per-service wire budget; no mutable process-global accounting."""

    def __init__(self, limit: int | None) -> None:
        self.limit = None if limit is None else max(0, int(limit))
        self.used = 0
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int | None:
        with self._lock:
            if self.limit is None:
                return None
            return max(0, self.limit - self.used)

    def reserve(self, requested: int) -> _ByteBudgetReservation:
        requested = max(1, int(requested))
        with self._lock:
            if self.limit is None:
                reserved = requested
            else:
                remaining = self.limit - self.used
                if remaining <= 0:
                    raise _ByteBudgetExceeded("取得全体のサイズ上限に達しました")
                reserved = min(requested, remaining)
            self.used += reserved
        return _ByteBudgetReservation(self, reserved)

    def settle(self, reserved: int, actual: int) -> None:
        # A response can be shorter than its reservation.  Keep the actual
        # bytes visible to callers and return only unused reservation capacity.
        unused = max(0, int(reserved) - max(0, int(actual)))
        if unused:
            with self._lock:
                self.used = max(0, self.used - unused)


class UrlIngestService:
    def __init__(
        self,
        *,
        timeout_seconds: float = 25.0,
        max_body_chars: int = 60_000,
        max_total_bytes: int | None = None,
        checkpoint: Callable[..., Any] | None = None,
        config: Any = None,
        privacy_gateway: OutboundPrivacyGateway | None = None,
        yahoo_search_client: DeepResearchSearchClient | None = None,
        session_context: Mapping[str, Any] | None = None,
        project_metadata: Mapping[str, Any] | None = None,
    ):
        self.timeout = httpx.Timeout(timeout_seconds, connect=8.0)
        self.max_body_chars = max_body_chars
        self.max_total_bytes = None if max_total_bytes is None else max(0, int(max_total_bytes))
        self._byte_budget = _ByteBudget(self.max_total_bytes)
        self.checkpoint = checkpoint
        self.config = config
        self.session_context = (
            dict(session_context) if isinstance(session_context, Mapping) else None
        )
        self.project_metadata = (
            dict(project_metadata) if isinstance(project_metadata, Mapping) else None
        )
        self._privacy_gateway = privacy_gateway or OutboundPrivacyGateway(
            config,
            session_context=self.session_context,
            project_metadata=self.project_metadata,
        )
        self._yahoo_search_client = yahoo_search_client or DeepResearchSearchClient(
            config=config,
            timeout_seconds=timeout_seconds,
            session_context=self.session_context,
            project_metadata=self.project_metadata,
        )
        # Reuse this request/service privacy scope for the shared Yahoo client;
        # otherwise a configured local_only policy could be lost on the nested
        # DeepResearchSearchClient boundary.
        if hasattr(self._yahoo_search_client, "_privacy_gateway"):
            self._yahoo_search_client._privacy_gateway = self._privacy_gateway
        self.headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AoiTalkClip/1.0; +https://github.com/)",
            "Accept-Language": "ja,en;q=0.8",
        }
        # The pinned transport is synchronous and runs in a worker thread.
        # Keep cancellation state on this service instance so cancelling one
        # expansion cannot affect another user's request or a different
        # service instance.
        self._cancel_event = threading.Event()
        self._active_connections_lock = threading.RLock()
        self._active_connections: set[Any] = set()

    def cancel(self) -> None:
        """Abort in-flight pinned requests and prevent new transport work."""

        self._cancel_event.set()
        with self._active_connections_lock:
            connections = list(self._active_connections)
        for connection in connections:
            self._abort_connection(connection)

    def close(self) -> None:
        """Close the service's in-flight transport context."""

        self.cancel()

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise _TransportCancelled("URL取得がキャンセルされました")

    @staticmethod
    def _abort_connection(connection: Any) -> None:
        """Shutdown and close a socket so a blocked worker read is released."""

        sock = (
            getattr(connection, "sock", None)
            or getattr(connection, "_aoi_pending_sock", None)
            or getattr(connection, "_aoi_transport_sock", None)
        )
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        try:
            connection.close()
        except Exception:
            pass

    @property
    def bytes_used(self) -> int:
        """Wire bytes observed by this instance, including error/redirect bodies."""

        return int(self._byte_budget.used)

    @property
    def bytes_remaining(self) -> int | None:
        """Remaining total wire budget, or ``None`` when unlimited."""

        return self._byte_budget.remaining

    async def _privacy_get(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        action: str = "url.fetch",
        source_kind: str = "url_fetch",
    ) -> httpx.Response:
        """Perform one URL-ingest GET through the privacy gateway.

        URL destinations and provider credentials are control-plane values;
        query parameters (notably oEmbed URLs and X/GitHub identifiers) are
        user-derived and therefore the payload protected by the gateway.
        ``sender`` owns the actual httpx call so a failed review cannot fall
        through to an unprotected request.
        """

        async def sender(protected_payload: Any) -> httpx.Response:
            if not isinstance(protected_payload, Mapping) or "params" not in protected_payload:
                raise PrivacyError("URL ingest reviewed payload is malformed")
            protected_params = protected_payload.get("params")
            if not isinstance(protected_params, Mapping):
                raise PrivacyError("URL ingest reviewed params are malformed")
            return await client.get(
                url,
                params=dict(protected_params),
                headers=dict(headers) if headers is not None else None,
                cookies=dict(cookies) if cookies is not None else None,
                follow_redirects=False,
            )

        return await self._privacy_gateway.execute(
            {"params": dict(params or {})},
            provider="url_ingest",
            descriptor=EgressDescriptor(
                action=action,
                transport="httpx",
                destination=url,
                provider="url_ingest",
            ),
            sender=sender,
            base_url=url,
            source_kind=source_kind,
        )

    async def fetch_all(
        self,
        urls: list[str],
        *,
        user_id: UUID | None = None,
        session: Any = None,
    ) -> list[UrlFetchResult]:
        # URL ingestion is often launched from a background clip worker, but
        # interactive requests do provide a user id.  Bind that identity to
        # the shared gateway so review-required content cannot silently use an
        # anonymous scope; the background path intentionally remains
        # fail-closed when no interactive session is present.
        if user_id is not None and not getattr(self._privacy_gateway, "user_id", ""):
            context = get_privacy_policy_context()
            session_context = context.session_context or self.session_context or {}
            session_id = str(
                session_context.get("session_id")
                or session_context.get("id")
                or ""
            )
            self._privacy_gateway = OutboundPrivacyGateway(
                self.config,
                user_id=str(user_id),
                session_id=session_id,
                session_context=context.session_context or self.session_context,
                project_metadata=context.project_metadata or self.project_metadata,
            )
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=False, headers=self.headers
        ) as client:
            if user_id is None and session is None:
                # Preserve the simple call shape used by direct callers and
                # older test doubles.
                return await asyncio.gather(*(self._fetch_one(client, url) for url in urls))
            x_cookie = None
            if user_id is not None and session is not None and any(
                (urlsplit(url).hostname or "").casefold() in _X_HOSTS for url in urls
            ):
                # One request-scoped DB lookup is shared by all URL tasks;
                # AsyncSession must not be used concurrently by each gather
                # branch.
                x_cookie = await resolve_x_cookie(session, user_id)
            return await asyncio.gather(
                *(
                    self._fetch_one(
                        client,
                        url,
                        user_id=user_id,
                        session=session,
                        x_cookie=x_cookie,
                    )
                    for url in urls
                )
            )

    async def _fetch_one(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        user_id: UUID | None = None,
        session: Any = None,
        x_cookie: XCookieResolution | None = None,
    ) -> UrlFetchResult:
        host = urlsplit(url).hostname or ""
        try:
            await self._assert_public_url(url)
            if host.casefold() in _X_HOSTS:
                return await self._fetch_x(
                    client,
                    url,
                    user_id=user_id,
                    session=session,
                    x_cookie=x_cookie,
                )
            if host.casefold() in _GITHUB_HOSTS:
                github = await self._fetch_github(client, url)
                if github.success:
                    return github
            if host.casefold() in _HUGGING_FACE_HOSTS:
                try:
                    hugging_face = await self._fetch_hugging_face(client, url)
                    if hugging_face.success:
                        return hugging_face
                except (httpx.HTTPError, ValueError):
                    pass
            if host.casefold() in _CIVITAI_HOSTS:
                civitai = await self._fetch_civitai(client, url)
                if civitai is not None:
                    return civitai
            return await self._fetch_html(client, url)
        except (httpx.HTTPError, ValueError) as exc:
            status = self._status_from_exception(exc)
            return UrlFetchResult(
                requested_url=url,
                final_url=url,
                error=self._status_message(status),
                acquisition_status=status,
            )
        except (ExternalProviderBlocked, PrivacyError):
            # Privacy/local-only denials are terminal for this URL.  Never
            # fall through to an alternate provider or direct HTML fetch.
            return UrlFetchResult(
                requested_url=url,
                final_url=url,
                error="外部送信がプライバシーポリシーで拒否されました",
                acquisition_status="privacy_blocked",
            )
        except Exception:  # URL単位で隔離し、全件の状態を必ず返す
            return UrlFetchResult(
                requested_url=url,
                final_url=url,
                error=self._status_message("fetch_failed"),
                acquisition_status="fetch_failed",
            )

    @staticmethod
    def _status_from_exception(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code == 401:
                return "auth_required"
            if status_code == 403:
                return "access_denied"
            if status_code in {404, 410}:
                return "deleted"
            if status_code == 429:
                return "rate_limited"
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return "network_error"
        if isinstance(exc, (OSError, http.client.HTTPException)):
            return "network_error"
        message = str(exc).casefold()
        if "サイズ上限" in str(exc) or "byte budget" in message:
            return "byte_budget_exhausted"
        if "content-type" in message:
            return "unsupported_content"
        if "encoding" in message or "compressed" in message or "圧縮" in str(exc):
            return "unsupported_content"
        if "本文" in str(exc) or "空" in str(exc):
            return "empty_body"
        return "fetch_failed"

    @staticmethod
    def _status_message(status: str) -> str:
        return {
            "auth_required": "認証が必要です",
            "access_denied": "アクセスが拒否されました",
            "restricted": "年齢またはセンシティブ設定により表示が制限されています",
            "private": "非公開コンテンツです",
            "rate_limited": "取得先のレート制限に達しました",
            "deleted": "削除済みまたは存在しないコンテンツです",
            "network_error": "ネットワーク接続に失敗しました",
            "empty_body": "取得応答に本文がありません",
            "unsupported_content": "本文として扱えない形式です",
            "byte_budget_exhausted": "取得全体のサイズ上限に達しました",
            "inaccessible": "公開本文を確認できないページです",
            "fetch_failed": "本文の取得に失敗しました",
        }.get(status, "本文の取得に失敗しました")

    async def _fetch_html(self, client: httpx.AsyncClient, url: str) -> UrlFetchResult:
        response = await self._safe_get(client, url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text/" not in content_type:
            raise ValueError(f"本文として扱えないContent-Typeです: {content_type or '不明'}")
        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        og_title = self._meta(soup, "property", "og:title")
        og_description = self._meta(soup, "property", "og:description")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        main = soup.find("article") or soup.find("main") or soup.body or soup
        body = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))[: self.max_body_chars]
        links = self._external_links(soup, str(response.url))
        if len(body.strip()) < 80:
            raise ValueError("本文を必要な水準で取得できませんでした")
        return UrlFetchResult(
            requested_url=url,
            final_url=str(response.url),
            success=True,
            title=title,
            og_title=og_title,
            og_description=og_description,
            body=body,
            external_links=links,
        )

    async def fetch_public(
        self,
        url: str,
        *,
        kind: str = "auto",
        max_body_chars: int | None = None,
    ) -> UrlFetchResult:
        """Fetch one public HTML/JSON URL through the pinned safe path.

        This method deliberately does not dispatch to the historical X,
        GitHub, Hugging Face, Civitai, or cookie-backed provider adapters.
        Research expansion uses it for public evidence only, so an article
        page or metadata endpoint cannot trigger a credential-provider
        shortcut by hostname.
        """

        requested_url = str(url or "").strip()
        if not requested_url:
            return UrlFetchResult(
                requested_url=requested_url,
                final_url=requested_url,
                error="URLが空です",
                acquisition_status="fetch_failed",
            )
        # ``_safe_get`` uses the pinned transport and privacy gateway and does
        # not depend on the AsyncClient instance.  A tiny client object keeps
        # the call shape compatible with older embedding/test doubles.
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            headers=self.headers,
        ) as client:
            try:
                response = await self._safe_get(client, requested_url)
                final_url = str(getattr(response, "url", "") or requested_url)
                content_type = str(response.headers.get("content-type", ""))
                status_code = int(getattr(response, "status_code", 0) or 0)
                response_headers = {
                    str(key).lower(): str(value)
                    for key, value in dict(getattr(response, "headers", {}) or {}).items()
                }
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    status = self._status_from_exception(exc)
                    error_body = getattr(response, "content", b"")
                    if isinstance(error_body, (bytes, bytearray)):
                        error_text = bytes(error_body).decode("utf-8", errors="replace")
                    else:
                        error_text = str(getattr(response, "text", "") or "")
                    return UrlFetchResult(
                        requested_url=requested_url,
                        final_url=final_url,
                        body=error_text[: max_body_chars or self.max_body_chars],
                        status_code=status_code,
                        response_headers=response_headers,
                        error=self._status_message(status),
                        acquisition_status=status,
                    )
                raw_body = getattr(response, "content", b"")
                if isinstance(raw_body, str):
                    raw_body = raw_body.encode("utf-8", errors="replace")
                if not isinstance(raw_body, (bytes, bytearray)):
                    raw_body = str(getattr(response, "text", "") or "").encode(
                        "utf-8", errors="replace"
                    )
                text = bytes(raw_body).decode("utf-8", errors="replace")
                requested_kind = str(kind or "auto").strip().lower()
                if requested_kind not in {"auto", "html", "json"}:
                    raise ValueError("公開URLの取得種別が不正です")
                is_json = requested_kind == "json" or (
                    requested_kind == "auto" and "json" in content_type.casefold()
                )
                if is_json:
                    try:
                        def reject_json_constant(value: str) -> None:
                            raise ValueError(f"JSON定数が不正です: {value}")

                        payload = json.loads(text, parse_constant=reject_json_constant)
                    except (TypeError, ValueError) as exc:
                        return UrlFetchResult(
                            requested_url=requested_url,
                            final_url=final_url,
                            body=text[: max_body_chars or self.max_body_chars],
                            status_code=status_code,
                            response_headers=response_headers,
                            error="公開JSONを解釈できませんでした",
                            acquisition_status="parse_failed",
                        )
                    return UrlFetchResult(
                        requested_url=requested_url,
                        final_url=final_url,
                        success=True,
                        provider="public_http",
                        body=text[: max_body_chars or self.max_body_chars],
                        json_data=payload,
                        status_code=status_code,
                        response_headers=response_headers,
                    )
                if "html" not in content_type.casefold() and "text/" not in content_type.casefold():
                    return UrlFetchResult(
                        requested_url=requested_url,
                        final_url=final_url,
                        body=text[: max_body_chars or self.max_body_chars],
                        status_code=status_code,
                        response_headers=response_headers,
                        error="本文として扱えない形式です",
                        acquisition_status="unsupported_content",
                    )
                soup = BeautifulSoup(text, "html.parser")
                title = soup.title.get_text(" ", strip=True) if soup.title else ""
                og_title = self._meta(soup, "property", "og:title")
                og_description = self._meta(soup, "property", "og:description")
                author = ""
                for attr, key in (
                    ("name", "author"),
                    ("name", "citation_author"),
                    ("property", "article:author"),
                    ("name", "twitter:creator"),
                ):
                    author = self._meta(soup, attr, key)
                    if author:
                        break
                published_at = ""
                for attr, key in (
                    ("property", "article:published_time"),
                    ("name", "citation_publication_date"),
                    ("name", "date"),
                ):
                    published_at = self._meta(soup, attr, key)
                    if published_at:
                        break
                citation_doi = self._meta(soup, "name", "citation_doi")
                # Keep link extraction before decomposing script/style nodes so
                # hostile script text cannot become a candidate URL.
                links = self._external_links(soup, final_url)
                doi_url = self._citation_doi_url(citation_doi)
                if doi_url and doi_url not in links:
                    links.insert(0, doi_url)
                for link in self._public_link_hints(soup, final_url):
                    if link not in links:
                        links.append(link)
                    if len(links) >= 100:
                        break
                for tag in soup(["script", "style", "noscript", "svg"]):
                    tag.decompose()
                main = soup.find("article") or soup.find("main") or soup.body or soup
                body = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
                body = body[: max_body_chars or self.max_body_chars]
                shell_text = f"{title}\n{body}".casefold()
                normalized_title = re.sub(r"\s+", " ", title).strip().casefold()
                title_shell = bool(
                    re.fullmatch(
                        r"(?:login|required sign in|sign in|log in|access denied|captcha|verify you are human|cloudflare(?: challenge)?|paywall|ログイン|認証してください)(?:\s*[-|:].*)?",
                        normalized_title,
                    )
                )
                body_length = len(body.strip())
                short_shell_markers = (
                    "sign in",
                    "log in",
                    "login",
                    "captcha",
                    "access denied",
                    "verify you are human",
                    "enable javascript",
                    "paywall",
                    "cloudflare",
                    "ログイン",
                    "認証してください",
                )
                has_auth_form = False
                for form in soup.find_all("form"):
                    form_text = form.get_text(" ", strip=True).casefold()
                    form_action = str(form.get("action") or "").casefold()
                    input_types = {
                        str(node.get("type") or "text").casefold()
                        for node in form.find_all("input")
                    }
                    if (
                        {"password", "email"} & input_types
                        or "login" in form_action
                        or "sign-in" in form_action
                        or any(marker in form_text for marker in ("sign in", "log in", "login", "captcha", "ログイン"))
                    ):
                        has_auth_form = True
                        break
                short_shell = body_length < 200 and any(
                    marker in shell_text for marker in short_shell_markers
                )
                form_shell = has_auth_form and body_length < 600
                if title_shell or short_shell or form_shell or body_length < 80:
                    return UrlFetchResult(
                        requested_url=requested_url,
                        final_url=final_url,
                        provider="public_http",
                        title=title,
                        og_title=og_title,
                        og_description=og_description,
                        body="",
                        author=author,
                        published_at=published_at,
                        external_links=links,
                        status_code=status_code,
                        response_headers=response_headers,
                        error="公開本文を確認できないページです",
                        acquisition_status="inaccessible" if body.strip() else "empty_body",
                    )
                return UrlFetchResult(
                    requested_url=requested_url,
                    final_url=final_url,
                    success=len(body.strip()) >= 80,
                    provider="public_http",
                    title=title,
                    og_title=og_title,
                    og_description=og_description,
                    body=body,
                    author=author,
                    published_at=published_at,
                    external_links=links,
                    status_code=status_code,
                    response_headers=response_headers,
                    acquisition_status="success" if len(body.strip()) >= 80 else "inaccessible",
                )
            except (ExternalProviderBlocked, PrivacyError):
                raise
            except (httpx.HTTPError, ValueError, OSError, http.client.HTTPException) as exc:
                status = self._status_from_exception(exc)
                return UrlFetchResult(
                    requested_url=requested_url,
                    final_url=requested_url,
                    error=self._status_message(status),
                    acquisition_status=status,
                )
            except asyncio.CancelledError:
                self.cancel()
                raise


    async def _fetch_github(self, client: httpx.AsyncClient, url: str) -> UrlFetchResult:
        parts = [part for part in urlsplit(url).path.split("/") if part]
        if len(parts) < 2:
            return await self._fetch_html(client, url)
        owner, repo = parts[0], parts[1].removesuffix(".git")
        api_url = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
        api = await self._privacy_get(
            client,
            api_url,
            action="github.repository.get",
            source_kind="github_api",
        )
        api.raise_for_status()
        meta = api.json()
        readme_text = ""
        readme = await self._privacy_get(
            client,
            f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/readme",
            headers={**self.headers, "Accept": "application/vnd.github.raw+json"},
            action="github.repository.readme",
            source_kind="github_api",
        )
        if readme.is_success:
            readme_text = readme.text[: self.max_body_chars]
        body = "\n\n".join(
            item for item in [str(meta.get("description") or ""), readme_text] if item.strip()
        )
        if len(body.strip()) < 40:
            raise ValueError("GitHubリポジトリの説明またはREADMEを取得できませんでした")
        return UrlFetchResult(
            requested_url=url,
            final_url=str(meta.get("html_url") or url),
            success=True,
            provider="github_api",
            title=str(meta.get("full_name") or repo),
            og_description=str(meta.get("description") or ""),
            body=body,
            external_links=[str(meta.get("homepage"))] if meta.get("homepage") else [],
        )

    async def _fetch_hugging_face(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> UrlFetchResult:
        """Hugging Faceのモデルページを公開APIとmodel cardから取得する。"""
        parts = [part for part in urlsplit(url).path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "models":
            owner, repo = parts[1], parts[2]
        elif len(parts) >= 2 and parts[0] not in {
            "datasets",
            "spaces",
            "docs",
            "blog",
            "pricing",
            "login",
            "join",
            "settings",
        }:
            owner, repo = parts[0], parts[1]
        else:
            return UrlFetchResult(requested_url=url)
        repo_id = f"{owner}/{repo}"
        encoded_id = f"{quote(owner, safe='')}/{quote(repo, safe='')}"
        api = await self._privacy_get(
            client,
            f"https://huggingface.co/api/models/{encoded_id}",
            action="huggingface.model.get",
            source_kind="huggingface_api",
        )
        api.raise_for_status()
        meta = api.json()
        readme_text = ""
        readme = await self._privacy_get(
            client,
            f"https://huggingface.co/{encoded_id}/resolve/main/README.md",
            action="huggingface.model.readme",
            source_kind="huggingface_api",
        )
        if readme.is_success:
            readme_text = readme.text[: self.max_body_chars]
        selected_meta = {
            key: meta.get(key)
            for key in (
                "modelId",
                "pipeline_tag",
                "library_name",
                "tags",
                "cardData",
                "downloads",
                "likes",
            )
            if meta.get(key) not in (None, "", [], {})
        }
        body = "\n\n".join(
            value
            for value in [
                json.dumps(selected_meta, ensure_ascii=False),
                readme_text,
            ]
            if value.strip()
        )
        if len(body.strip()) < 80:
            raise ValueError(
                "Hugging Faceのモデル情報またはREADMEを取得できませんでした"
            )
        return UrlFetchResult(
            requested_url=url,
            final_url=f"https://huggingface.co/{repo_id}",
            success=True,
            provider="huggingface_api",
            title=str(meta.get("modelId") or repo_id),
            og_description=str(
                (meta.get("cardData") or {}).get("model_name")
                if isinstance(meta.get("cardData"), dict)
                else ""
            ),
            body=body[: self.max_body_chars],
            external_links=[
                f"https://huggingface.co/{repo_id}/blob/main/README.md"
            ],
        )

    async def _fetch_civitai(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> UrlFetchResult | None:
        """Civitaiのモデルページを公開REST APIから取得する。HTMLとCookieは使わない。"""
        ref = self._parse_civitai_ref(url)
        if ref is None:
            return None
        model: dict[str, Any] | None = None
        version: dict[str, Any] | None = None
        if ref.model_id is not None:
            response = await self._privacy_get(
                client,
                f"{_CIVITAI_API_ORIGIN}/api/v1/models/{ref.model_id}",
                action="civitai.model.get",
                source_kind="civitai_api",
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Civitaiモデル情報を取得できませんでした")
            model = payload
        if ref.version_id is not None and (
            model is None or not self._civitai_version_in_model(model, ref.version_id)
        ):
            try:
                response = await self._privacy_get(
                    client,
                    f"{_CIVITAI_API_ORIGIN}/api/v1/model-versions/{ref.version_id}",
                    action="civitai.model_version.get",
                    source_kind="civitai_api",
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                if model is None:
                    raise
                payload = None
            if isinstance(payload, dict) and self._civitai_version_belongs(payload, model, ref):
                version = payload
                if model is None:
                    model_id = payload.get("modelId")
                    if not isinstance(model_id, int):
                        raise ValueError("Civitaiモデル情報を取得できませんでした")
                    response = await self._privacy_get(
                        client,
                        f"{_CIVITAI_API_ORIGIN}/api/v1/models/{model_id}",
                        action="civitai.model.get",
                        source_kind="civitai_api",
                    )
                    response.raise_for_status()
                    model_payload = response.json()
                    if not isinstance(model_payload, dict):
                        raise ValueError("Civitaiモデル情報を取得できませんでした")
                    model = model_payload
        if model is None:
            raise ValueError("Civitaiモデル情報を取得できませんでした")
        if version is None:
            version = self._civitai_selected_version(model, ref.version_id)
        model_id = model.get("id") if isinstance(model.get("id"), int) else ref.model_id
        version_id = (
            version.get("id")
            if isinstance(version, dict) and isinstance(version.get("id"), int)
            else ref.version_id
        )
        creator = model.get("creator") if isinstance(model.get("creator"), dict) else {}
        author = str(creator.get("username") or "").strip()
        description = self._html_to_text(str(model.get("description") or ""))
        version_description = (
            self._html_to_text(str(version.get("description") or ""))
            if isinstance(version, dict)
            else ""
        )
        selected_meta = {
            key: value
            for key, value in {
                "id": model_id,
                "name": model.get("name"),
                "type": model.get("type"),
                "creator": author or None,
                "tags": model.get("tags"),
                "baseModels": model.get("baseModels"),
                "nsfw": model.get("nsfw"),
                "allowCommercialUse": model.get("allowCommercialUse"),
                "allowDerivatives": model.get("allowDerivatives"),
                "stats": self._civitai_stats(model.get("stats")),
                "version": self._civitai_version_meta(version) if version else None,
            }.items()
            if value not in (None, "", [], {})
        }
        body = "\n\n".join(
            value
            for value in [
                json.dumps(selected_meta, ensure_ascii=False),
                description,
                version_description,
            ]
            if value.strip()
        )
        if len(body.strip()) < 80:
            raise ValueError("Civitaiのモデル情報を取得できませんでした")
        title = str(model.get("name") or "").strip() or f"Civitai model {model_id}"
        version_name = str(version.get("name") or "").strip() if isinstance(version, dict) else ""
        if version_name:
            title = f"{title} - {version_name}"
        return UrlFetchResult(
            requested_url=url,
            final_url=self._civitai_page_url(ref.host, model_id, ref.slug, version_id),
            success=True,
            provider="civitai_api",
            title=title,
            og_title=title,
            og_description=description[:400],
            body=body[: self.max_body_chars],
            author=author,
            published_at=str(
                (version or {}).get("publishedAt") or model.get("publishedAt") or ""
            )
            if isinstance(version, dict) or model.get("publishedAt")
            else "",
            external_links=[
                self._civitai_page_url("civitai.com", model_id, ref.slug, version_id)
            ],
        )

    @staticmethod
    def _parse_civitai_ref(url: str) -> _CivitaiRef | None:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold().rstrip(".")
        if host not in _CIVITAI_HOSTS:
            return None
        segments = [segment for segment in parts.path.split("/") if segment]
        model_id: int | None = None
        version_id: int | None = None
        slug: str | None = None
        if (
            len(segments) >= 2
            and segments[0].casefold() == "models"
            and segments[1].isdigit()
        ):
            model_id = int(segments[1])
            if len(segments) >= 3 and _CIVITAI_SLUG_RE.fullmatch(segments[2]):
                slug = segments[2]
        elif (
            len(segments) >= 4
            and segments[0].casefold() == "api"
            and segments[1].casefold() == "v1"
            and segments[2].casefold() == "models"
            and segments[3].isdigit()
        ):
            model_id = int(segments[3])
        elif (
            len(segments) >= 4
            and segments[0].casefold() == "api"
            and segments[1].casefold() == "v1"
            and segments[2].casefold() == "model-versions"
            and segments[3].isdigit()
        ):
            version_id = int(segments[3])
        raw_version = (parse_qs(parts.query).get("modelVersionId") or [""])[0]
        if str(raw_version).isdigit():
            version_id = int(raw_version)
        if model_id is None and version_id is None:
            return None
        return _CivitaiRef(
            host=host,
            model_id=model_id,
            version_id=version_id,
            slug=slug,
        )

    @staticmethod
    def _civitai_page_url(
        host: str,
        model_id: int | None,
        slug: str | None,
        version_id: int | None,
    ) -> str:
        safe_host = host if host in _CIVITAI_HOSTS else "civitai.com"
        if model_id is None:
            return f"https://{safe_host}/"
        path = f"/models/{model_id}"
        if slug and _CIVITAI_SLUG_RE.fullmatch(slug):
            path += f"/{slug}"
        page = f"https://{safe_host}{path}"
        if version_id is not None:
            page += f"?modelVersionId={version_id}"
        return page

    @staticmethod
    def _civitai_version_in_model(model: dict[str, Any], version_id: int) -> bool:
        versions = model.get("modelVersions")
        if not isinstance(versions, list):
            return False
        return any(
            isinstance(item, dict) and item.get("id") == version_id for item in versions
        )

    @staticmethod
    def _civitai_version_belongs(
        version: dict[str, Any],
        model: dict[str, Any] | None,
        ref: _CivitaiRef,
    ) -> bool:
        model_id = version.get("modelId")
        if model is not None:
            return model_id in {model.get("id"), ref.model_id}
        return model_id == ref.model_id or ref.model_id is None

    @staticmethod
    def _civitai_selected_version(
        model: dict[str, Any],
        version_id: int | None,
    ) -> dict[str, Any] | None:
        versions = [
            item
            for item in (model.get("modelVersions") or [])
            if isinstance(item, dict)
        ]
        if version_id is not None:
            matched = next((item for item in versions if item.get("id") == version_id), None)
            if matched is not None:
                return matched
        return versions[0] if versions else None

    @staticmethod
    def _civitai_stats(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        stats = {
            key: value.get(key)
            for key in ("downloadCount", "thumbsUpCount", "commentCount")
            if value.get(key) not in (None, "")
        }
        return stats or None

    @classmethod
    def _civitai_version_meta(cls, version: dict[str, Any]) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        for item in version.get("files") or []:
            if not isinstance(item, dict):
                continue
            hashes = item.get("hashes") if isinstance(item.get("hashes"), dict) else {}
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            file_meta = {
                key: file_value
                for key, file_value in {
                    "name": item.get("name"),
                    "sizeKB": item.get("sizeKB"),
                    "type": item.get("type"),
                    "format": metadata.get("format"),
                    "fp": metadata.get("fp"),
                    "sha256": hashes.get("SHA256") or hashes.get("AutoV2"),
                    "primary": True if item.get("primary") else None,
                }.items()
                if file_value not in (None, "")
            }
            if file_meta:
                files.append(file_meta)
        return {
            key: value
            for key, value in {
                "id": version.get("id"),
                "name": version.get("name"),
                "baseModel": version.get("baseModel"),
                "publishedAt": version.get("publishedAt"),
                "files": files,
            }.items()
            if value not in (None, "", [])
        }

    @staticmethod
    def _html_to_text(value: str) -> str:
        soup = BeautifulSoup(value or "", "html.parser")
        return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True)).strip()

    @staticmethod
    async def _resolve_public_url(url: str) -> tuple[Any, str]:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("http/httpsの公開URLではありません")
        if parts.username is not None or parts.password is not None:
            raise ValueError("URLの認証情報は許可されていません")
        host = parts.hostname.casefold().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise ValueError("ローカルアドレスは取得できません")
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo, host, parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError(f"ホスト名を解決できません: {host}") from exc
        public_addresses: list[str] = []
        for address in {item[4][0] for item in addresses}:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not ip.is_global:
                raise ValueError("プライベートまたは予約済みアドレスは取得できません")
            public_addresses.append(address)
        if not public_addresses:
            raise ValueError("公開IPアドレスを解決できません")
        return parts, public_addresses[0]

    @classmethod
    async def _assert_public_url(cls, url: str) -> None:
        await cls._resolve_public_url(url)

    @staticmethod
    def _verified_ssl_context() -> ssl.SSLContext:
        """既定の信頼ストアを保ちつつ、最新の公開CAも検証候補へ追加する。"""
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=certifi.where())
        return context

    @staticmethod
    def _register_connection(
        connection: Any,
        *,
        active_connections: set[Any] | None,
        active_connections_lock: threading.RLock | None,
        cancel_event: threading.Event | None,
    ) -> None:
        if active_connections is None or active_connections_lock is None:
            if cancel_event is not None and cancel_event.is_set():
                raise _TransportCancelled("URL取得がキャンセルされました")
            return
        with active_connections_lock:
            active_connections.add(connection)
            cancelled = bool(cancel_event is not None and cancel_event.is_set())
        if cancelled:
            try:
                UrlIngestService._abort_connection(connection)
            finally:
                raise _TransportCancelled("URL取得がキャンセルされました")

    @staticmethod
    def _unregister_connection(
        connection: Any,
        *,
        active_connections: set[Any] | None,
        active_connections_lock: threading.RLock | None,
    ) -> None:
        if active_connections is None or active_connections_lock is None:
            return
        with active_connections_lock:
            active_connections.discard(connection)

    @staticmethod
    def _pinned_request(
        parts,
        address: str,
        *,
        max_bytes: int | None = None,
        reservation: _ByteBudgetReservation | None = None,
        cancel_event: threading.Event | None = None,
        active_connections: set[Any] | None = None,
        active_connections_lock: threading.RLock | None = None,
    ) -> httpx.Response:
        """Issue one DNS-pinned GET and account every received byte.

        The historical default remains an 8 MiB response cap.  When a caller
        supplies a reservation, the reservation is made before this function
        runs in a worker thread and the read cap includes one probe byte when
        capacity allows, so a single oversized response is detected without
        exceeding the total budget.
        """

        host = str(parts.hostname)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        actual_read = 0
        connection: Any = None

        def is_cancelled() -> bool:
            return bool(cancel_event is not None and cancel_event.is_set())

        def track_pending_socket(conn: Any, sock: Any) -> None:
            if active_connections_lock is None:
                if is_cancelled():
                    sock.close()
                    raise _TransportCancelled("URL取得がキャンセルされました")
                conn._aoi_pending_sock = sock
                return
            with active_connections_lock:
                cancelled = is_cancelled()
                if not cancelled:
                    conn._aoi_pending_sock = sock
            if cancelled:
                sock.close()
                raise _TransportCancelled("URL取得がキャンセルされました")

        def attach_connected_socket(conn: Any, sock: Any) -> None:
            if active_connections_lock is None:
                if is_cancelled():
                    sock.close()
                    raise _TransportCancelled("URL取得がキャンセルされました")
                conn.sock = sock
                conn._aoi_transport_sock = sock
                conn._aoi_pending_sock = None
                return
            with active_connections_lock:
                cancelled = is_cancelled()
                if not cancelled:
                    conn.sock = sock
                    conn._aoi_transport_sock = sock
                    conn._aoi_pending_sock = None
            if cancelled:
                UrlIngestService._abort_connection(conn)
                raise _TransportCancelled("URL取得がキャンセルされました")

        def pinned_connect(conn: Any, *, use_tls: bool) -> None:
            if is_cancelled():
                raise _TransportCancelled("URL取得がキャンセルされました")
            tls_context = conn._context if use_tls else None
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setblocking(False)
            track_pending_socket(conn, sock)
            try:
                if family == socket.AF_INET6:
                    scope = address.partition("%")[2]
                    try:
                        scope_id = int(scope) if scope else 0
                    except ValueError:
                        scope_id = socket.if_nametoindex(scope)
                    sockaddr = (address.split("%", 1)[0], port, 0, scope_id)
                else:
                    sockaddr = (address, port)
                if is_cancelled():
                    raise _TransportCancelled("URL取得がキャンセルされました")
                # Poll nonblocking connect in short intervals so cancellation
                # does not depend on whether Windows interrupts a blocking
                # connect when another thread closes the socket.
                connect_error = sock.connect_ex(sockaddr)
                pending_errors = {
                    errno.EINPROGRESS,
                    errno.EALREADY,
                    errno.EWOULDBLOCK,
                    errno.EINTR,
                    10035,  # WSAEWOULDBLOCK
                    10036,  # WSAEINPROGRESS
                    10037,  # WSAEALREADY
                }
                connected_errors = {0, errno.EISCONN, 10056}  # WSAEISCONN
                deadline = time.monotonic() + 25.0
                while connect_error not in connected_errors:
                    if is_cancelled():
                        raise _TransportCancelled("URL取得がキャンセルされました")
                    if connect_error not in pending_errors:
                        raise OSError(connect_error, os.strerror(connect_error))
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise socket.timeout("公開URL接続がタイムアウトしました")
                    _, writable, exceptional = select.select(
                        [], [sock], [sock], min(0.1, remaining)
                    )
                    if is_cancelled():
                        raise _TransportCancelled("URL取得がキャンセルされました")
                    if exceptional:
                        connect_error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    elif writable:
                        connect_error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        if connect_error == 0:
                            break
                    if connect_error and connect_error not in pending_errors:
                        raise OSError(connect_error, os.strerror(connect_error))
                sock.settimeout(25)
                if is_cancelled():
                    raise _TransportCancelled("URL取得がキャンセルされました")
                if use_tls:
                    if active_connections_lock is None:
                        if is_cancelled():
                            raise _TransportCancelled("URL取得がキャンセルされました")
                        secure_sock = tls_context.wrap_socket(
                            sock,
                            server_hostname=host,
                            do_handshake_on_connect=False,
                        )
                        conn.sock = secure_sock
                        conn._aoi_transport_sock = secure_sock
                        conn._aoi_pending_sock = None
                    else:
                        with active_connections_lock:
                            if is_cancelled():
                                cancelled = True
                            else:
                                secure_sock = tls_context.wrap_socket(
                                    sock,
                                    server_hostname=host,
                                    do_handshake_on_connect=False,
                                )
                                conn.sock = secure_sock
                                conn._aoi_transport_sock = secure_sock
                                conn._aoi_pending_sock = None
                                cancelled = is_cancelled()
                        if cancelled:
                            UrlIngestService._abort_connection(conn)
                            raise _TransportCancelled("URL取得がキャンセルされました")
                    secure_sock.setblocking(False)
                    tls_deadline = time.monotonic() + 25.0
                    while True:
                        if is_cancelled():
                            raise _TransportCancelled("URL取得がキャンセルされました")
                        try:
                            secure_sock.do_handshake()
                            break
                        except ssl.SSLWantReadError:
                            read_socks, write_socks = [secure_sock], []
                        except ssl.SSLWantWriteError:
                            read_socks, write_socks = [], [secure_sock]
                        remaining = tls_deadline - time.monotonic()
                        if remaining <= 0:
                            raise socket.timeout("TLS接続がタイムアウトしました")
                        readable, writable, exceptional = select.select(
                            read_socks, write_socks, [secure_sock], min(0.1, remaining)
                        )
                        if is_cancelled():
                            raise _TransportCancelled("URL取得がキャンセルされました")
                        if exceptional:
                            raise OSError("TLSソケット接続に失敗しました")
                        if not readable and not writable:
                            continue
                    secure_sock.settimeout(25)
                    if is_cancelled():
                        raise _TransportCancelled("URL取得がキャンセルされました")
                else:
                    attach_connected_socket(conn, sock)
            except BaseException:
                UrlIngestService._abort_connection(conn)
                raise

        def pinned_send(conn: Any, data: Any) -> None:
            if conn.sock is None:
                conn.connect()
            if active_connections_lock is None:
                if is_cancelled():
                    raise _TransportCancelled("URL取得がキャンセルされました")
                sock = conn.sock
            else:
                with active_connections_lock:
                    if is_cancelled():
                        raise _TransportCancelled("URL取得がキャンセルされました")
                    sock = conn.sock
            if sock is None:
                if is_cancelled():
                    raise _TransportCancelled("URL取得がキャンセルされました")
                raise OSError("接続ソケットがありません")
            if isinstance(data, str):
                data = data.encode("ascii")
            try:
                sock.sendall(data)
            except OSError:
                if is_cancelled():
                    raise _TransportCancelled("URL取得がキャンセルされました") from None
                raise

        try:
            if parts.scheme == "https":
                class PinnedHttpsConnection(http.client.HTTPSConnection):
                    def connect(self):
                        pinned_connect(self, use_tls=True)

                    def send(self, data):
                        pinned_send(self, data)

                connection = PinnedHttpsConnection(
                    host,
                    port=port,
                    timeout=25,
                    context=UrlIngestService._verified_ssl_context(),
                )
            else:
                class PinnedHttpConnection(http.client.HTTPConnection):
                    def connect(self):
                        pinned_connect(self, use_tls=False)

                    def send(self, data):
                        pinned_send(self, data)

                connection = PinnedHttpConnection(address, port=port, timeout=25)
            UrlIngestService._register_connection(
                connection,
                active_connections=active_connections,
                active_connections_lock=active_connections_lock,
                cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                raise _TransportCancelled("URL取得がキャンセルされました")
            connection.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", host if parts.port is None else f"{host}:{parts.port}")
            connection.putheader("User-Agent", "Mozilla/5.0 (compatible; AoiTalkClip/1.0)")
            connection.putheader("Accept-Language", "ja,en;q=0.8")
            connection.putheader("Accept-Encoding", "identity")
            connection.endheaders()
            if cancel_event is not None and cancel_event.is_set():
                raise _TransportCancelled("URL取得がキャンセルされました")
            raw = connection.getresponse()
            response_limit = 8 * 1024 * 1024
            if max_bytes is None:
                read_limit = response_limit + 1
                exact_cap_requires_length = False
            else:
                budget_limit = max(0, int(max_bytes))
                response_limit = min(response_limit, budget_limit)
                # A one-byte probe is available only when the remaining
                # instance budget is larger than the per-response cap.
                read_limit = response_limit + 1 if budget_limit > response_limit else budget_limit
                exact_cap_requires_length = budget_limit <= response_limit

            chunks: list[bytes] = []
            remaining = read_limit
            while remaining > 0:
                if cancel_event is not None and cancel_event.is_set():
                    raise _TransportCancelled("URL取得がキャンセルされました")
                try:
                    chunk = raw.read(min(64 * 1024, remaining))
                except (OSError, http.client.HTTPException):
                    if is_cancelled():
                        raise _TransportCancelled("URL取得がキャンセルされました") from None
                    raise
                if is_cancelled():
                    raise _TransportCancelled("URL取得がキャンセルされました")
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                chunk = bytes(chunk)
                chunks.append(chunk)
                actual_read += len(chunk)
                remaining -= len(chunk)
                if len(chunk) > 64 * 1024:
                    # A non-conforming test double or response object must not
                    # make the loop read beyond the reservation.
                    remaining = max(0, remaining)
            content = b"".join(chunks)
            content_length_header = raw.getheader("Content-Length")
            content_encoding = str(raw.getheader("Content-Encoding") or "").strip().casefold()
            if max_bytes is not None and content_encoding not in {"", "identity"}:
                raise ValueError("圧縮された取得本文は扱えません")
            content_length: int | None = None
            if content_length_header not in (None, ""):
                try:
                    content_length = int(str(content_length_header))
                except (TypeError, ValueError) as exc:
                    raise ValueError("取得本文のサイズ情報が不正です") from exc
                if content_length < 0:
                    raise ValueError("取得本文のサイズ情報が不正です")
                if content_length > response_limit:
                    raise ValueError("取得本文がサイズ上限を超えました")
                if actual_read < content_length:
                    raise ValueError("取得本文が不完全です")
                if actual_read > content_length:
                    raise ValueError("取得本文のサイズ情報が不正です")
            if len(content) > response_limit:
                raise ValueError("取得本文がサイズ上限を超えました")
            if exact_cap_requires_length and len(content) == response_limit:
                # With no probe byte remaining, an unknown-length response
                # filling the cap could be truncated at the total budget.
                if content_length is None:
                    raise ValueError("取得本文がサイズ上限を超えた可能性があります")
            url = parts.geturl()
            return httpx.Response(
                raw.status, headers=dict(raw.getheaders()), content=content,
                request=httpx.Request("GET", url),
            )
        finally:
            if reservation is not None:
                reservation.settle(actual_read)
            if connection is not None:
                UrlIngestService._unregister_connection(
                    connection,
                    active_connections=active_connections,
                    active_connections_lock=active_connections_lock,
                )
                UrlIngestService._abort_connection(connection)

    async def _safe_get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """各redirect先を接続前に検証し、SSRFを防ぐ。

        The low-level pinned request is still a real external egress, so each
        hop is its own gateway transaction.  Redirects are never delegated to
        httpx (which is configured with ``follow_redirects=False``); every hop
        is revalidated and reviewed independently.
        """
        current = url
        visited: set[str] = set()
        for _ in range(8):
            if self._cancel_event.is_set():
                raise asyncio.CancelledError
            await self._run_checkpoint("url_http_hop")
            # Redirect cycles are bounded as an explicit condition, rather
            # than spending all eight hops on the same two destinations.
            current_key = UrlIngestService._canonical_public_url(current)
            if current_key in visited:
                raise ValueError("リダイレクト循環を検出しました")
            visited.add(current_key)
            ensure_allowed = getattr(self._privacy_gateway, "ensure_provider_allowed", None)
            if callable(ensure_allowed):
                ensure_allowed("url_ingest", base_url=current)
            reservation: _ByteBudgetReservation | None = None
            max_bytes: int | None = None
            if self.max_total_bytes is not None:
                # Reserve before starting the worker thread.  The per-response
                # cap is still 8 MiB; a small remaining total budget narrows
                # the read to the remaining capacity.
                reservation = self._byte_budget.reserve(8 * 1024 * 1024 + 1)
                max_bytes = reservation.reserved
            try:
                parts, address = await self._resolve_public_url(current)
            except BaseException:
                if reservation is not None:
                    reservation.settle(0)
                raise

            request_started = threading.Event()

            async def sender(_protected_payload: Any) -> httpx.Response:
                self._raise_if_cancelled()
                request_started.set()
                pinned = self._pinned_request
                kwargs: dict[str, Any] = {}
                # Keep narrow test doubles and existing embedders callable
                # while passing the cancellation context to the canonical
                # transport implementation.
                try:
                    parameters = inspect.signature(pinned).parameters
                    accepts_kwargs = any(
                        item.kind is inspect.Parameter.VAR_KEYWORD
                        for item in parameters.values()
                    )
                except (TypeError, ValueError):
                    parameters = {}
                    accepts_kwargs = True
                optional = {
                    "max_bytes": max_bytes,
                    "reservation": reservation,
                    "cancel_event": self._cancel_event,
                    "active_connections": self._active_connections,
                    "active_connections_lock": self._active_connections_lock,
                }
                for name, value in optional.items():
                    if accepts_kwargs or name in parameters:
                        if value is not None:
                            kwargs[name] = value
                worker = asyncio.create_task(
                    asyncio.to_thread(pinned, parts, address, **kwargs)
                )
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # Shield keeps the underlying thread future alive while
                    # the connection is shut down.  Await it briefly so a
                    # cancelled fetch does not leave a worker holding the
                    # service's reservation or active-connection slot.
                    self.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(worker), timeout=1.0)
                    except BaseException:
                        worker.add_done_callback(_consume_transport_task)
                    raise

            try:
                response = await self._privacy_gateway.execute(
                    {},
                    provider="url_ingest",
                    descriptor=EgressDescriptor(
                        action="url.http.get",
                        transport="http.client",
                        destination=current,
                        provider="url_ingest",
                    ),
                    sender=sender,
                    base_url=current,
                    source_kind="url_http_fetch",
                )
            except (ExternalProviderBlocked, PrivacyError):
                if reservation is not None and not request_started.is_set():
                    # No transport bytes were observed when the gateway denied
                    # the request before invoking the sender.
                    reservation.settle(0)
                raise
            except _TransportCancelled as exc:
                if reservation is not None and not request_started.is_set():
                    reservation.settle(0)
                raise asyncio.CancelledError from exc
            except asyncio.CancelledError:
                # ``asyncio.to_thread`` cannot stop a running synchronous
                # reader by itself.  Close the registered connection before
                # propagating cancellation so the worker exits promptly.
                self.cancel()
                raise
            except Exception:
                if reservation is not None and not request_started.is_set():
                    reservation.settle(0)
                raise
            else:
                if reservation is not None:
                    actual = len(getattr(response, "content", b"") or b"")
                    reservation.settle(actual)
            if self._cancel_event.is_set():
                raise asyncio.CancelledError
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return response
                current = urljoin(current, location)
                continue
            return response
        raise ValueError("リダイレクト回数が上限を超えました")

    async def _run_checkpoint(self, stage: str) -> None:
        if self._cancel_event.is_set():
            raise asyncio.CancelledError
        callback = self.checkpoint
        if callback is None:
            return
        try:
            signature = inspect.signature(callback)
            accepts_stage = any(
                parameter.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.VAR_POSITIONAL,
                )
                for parameter in signature.parameters.values()
            )
        except (TypeError, ValueError):
            accepts_stage = True
        value = callback(stage) if accepts_stage else callback()
        if inspect.isawaitable(value):
            value = await value
        if value is False:
            self.cancel()
            raise asyncio.CancelledError

    @staticmethod
    def _canonical_public_url(url: str) -> str:
        """Normalize only scheme/hostname and remove fragments for loop checks."""

        parts = urlsplit(str(url or ""))
        hostname = (parts.hostname or "").casefold().rstrip(".")
        if not hostname:
            return str(url or "")
        netloc = hostname
        if parts.port is not None:
            netloc = f"{hostname}:{parts.port}"
        return parts._replace(
            scheme=parts.scheme.casefold(),
            netloc=netloc,
            fragment="",
        ).geturl()

    @staticmethod
    def _canonical_x_url(url: str) -> str:
        """Return the shared service's credential-free X URL form."""

        canonical = canonicalize_x_url(url)
        if not canonical or not x_status_id(canonical):
            raise ValueError("X投稿IDをURLから判別できません")
        return canonical

    async def _fetch_x_from_yahoo(
        self,
        client: httpx.AsyncClient,
        *,
        requested_url: str,
        canonical_url: str,
        tweet_id: str,
    ) -> UrlFetchResult | None:
        """Fetch one exact status from the shared Yahoo realtime boundary."""

        base_url = self._yahoo_search_client._yahoo_realtime_url()
        # URL ingestion has no caller-supplied config in the historical API,
        # but still participates in the same local_only policy boundary.
        self._privacy_gateway.ensure_provider_allowed(
            "yahoo_realtime",
            base_url=base_url,
        )
        search = getattr(self._yahoo_search_client, "search_yahoo_realtime", None)
        if not callable(search):
            search = getattr(self._yahoo_search_client, "_search_yahoo_realtime", None)
        if not callable(search):
            return None
        result = await search(client, canonical_url, limit=10)
        # DeepResearchSearchClient returns citation sources; accept a shared
        # result envelope/list as well so this remains test-double friendly.
        if hasattr(result, "posts"):
            candidates = list(getattr(result, "posts", ()) or ())
            status = str(getattr(result, "status", "success") or "success")
            if status in {"blocked", "privacy_blocked"}:
                raise ExternalProviderBlocked("Yahoo realtime search was blocked")
        elif isinstance(result, dict):
            candidates = result.get("posts") or result.get("results") or []
            status = str(result.get("status") or "success")
            if status in {"blocked", "privacy_blocked"}:
                raise ExternalProviderBlocked("Yahoo realtime search was blocked")
        else:
            candidates = list(result or ())
        for item in candidates:
            if isinstance(item, DeepResearchSource):
                source_url = item.url
                title = item.title
                body = item.snippet
                author = str(item.raw.get("author") or "")
                published = str(item.published_at or item.raw.get("published_at") or "")
                raw = item.raw
            elif isinstance(item, dict):
                source_url = str(item.get("url") or item.get("canonical_url") or "")
                title = str(item.get("title") or "")
                body = str(item.get("text") or item.get("body") or item.get("snippet") or "")
                author = str(
                    item.get("author")
                    or item.get("author_name")
                    or item.get("author_handle")
                    or ""
                )
                published = str(item.get("published_at") or item.get("created_at") or "")
                raw = item
            else:
                source_url = str(getattr(item, "url", "") or getattr(item, "canonical_url", "") or "")
                title = str(getattr(item, "title", "") or "")
                body = str(
                    getattr(item, "text", "")
                    or getattr(item, "body", "")
                    or getattr(item, "snippet", "")
                    or ""
                )
                author = str(
                    getattr(item, "author", "")
                    or getattr(item, "author_name", "")
                    or getattr(item, "author_handle", "")
                    or ""
                )
                published = str(getattr(item, "published_at", "") or "")
                raw = getattr(item, "raw", {}) or {}
            # Yahoo can return neighboring statuses.  Only the exact numeric
            # status ID is acceptable; no title/text similarity fallback.
            if x_status_id(source_url) != tweet_id:
                continue
            body = re.sub(r"\s+", " ", body or "").strip()
            if not body:
                continue
            return UrlFetchResult(
                requested_url=requested_url,
                final_url=canonical_url,
                success=True,
                provider="yahoo_realtime",
                title=title or f"X投稿 {tweet_id}",
                body=body[: self.max_body_chars],
                author=author,
                published_at=published,
                error="",
            )
        return None

    async def _fetch_x(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        user_id: UUID | None = None,
        session: Any = None,
        x_cookie: XCookieResolution | None = None,
    ) -> UrlFetchResult:
        canonical_url = self._canonical_x_url(url)
        tweet_id = x_status_id(canonical_url) or ""
        observed_statuses: list[str] = []
        # Yahoo realtime is the first, shared public boundary for X posts.  A
        # neighboring result must never be accepted: _fetch_x_from_yahoo
        # compares the exact numeric status ID before constructing success.
        try:
            yahoo_result = await self._fetch_x_from_yahoo(
                client,
                requested_url=url,
                canonical_url=canonical_url,
                tweet_id=tweet_id,
            )
            if yahoo_result is not None:
                return yahoo_result
            observed_statuses.append("empty_body")
        except (ExternalProviderBlocked, PrivacyError):
            # local_only/review failures are a privacy boundary, not a reason
            # to silently fall through to other external X providers.
            raise
        except Exception as exc:
            observed_statuses.append(self._status_from_exception(exc))
        # 公開syndicationは本文・投稿者・日時・引用・mediaを構造化して返す。
        try:
            response = await self._privacy_get(
                client,
                "https://cdn.syndication.twimg.com/tweet-result",
                params={"id": tweet_id, "lang": "ja"},
                action="x.syndication.get",
                source_kind="x_syndication",
            )
            response.raise_for_status()
            data = response.json()
            text = str(data.get("text") or "").strip()
            if text:
                user = data.get("user") if isinstance(data.get("user"), dict) else {}
                quoted = data.get("quoted_tweet") if isinstance(data.get("quoted_tweet"), dict) else {}
                media = data.get("mediaDetails") if isinstance(data.get("mediaDetails"), list) else []
                return UrlFetchResult(
                    requested_url=url,
                    final_url=url,
                    success=True,
                    provider="x_syndication",
                    title=f"X投稿 by {user.get('name') or user.get('screen_name') or 'unknown'}",
                    body=text,
                    author=str(user.get("name") or user.get("screen_name") or ""),
                    published_at=str(data.get("created_at") or ""),
                    quoted_post=str(quoted.get("text") or ""),
                    external_links=[str(item.get("expanded_url")) for item in data.get("entities", {}).get("urls", []) if isinstance(item, dict) and item.get("expanded_url")],
                    media_descriptions=[str(item.get("ext_alt_text") or item.get("type") or "") for item in media if isinstance(item, dict)],
                )
            observed_statuses.append(self._x_payload_status(data))
        except Exception as exc:
            observed_statuses.append(self._status_from_exception(exc))
        # oEmbedは認証不要の別経路。本文を含むblockquoteだけを成功とする。
        try:
            response = await self._privacy_get(
                client,
                "https://publish.twitter.com/oembed",
                params={"url": url, "omit_script": "true"},
                action="x.oembed.get",
                source_kind="x_oembed",
            )
            response.raise_for_status()
            data = response.json()
            soup = BeautifulSoup(html.unescape(str(data.get("html") or "")), "html.parser")
            paragraph = soup.find("p")
            text = paragraph.get_text(" ", strip=True) if paragraph else ""
            if text:
                return UrlFetchResult(
                    requested_url=url, final_url=url, success=True, provider="x_oembed",
                    title=f"X投稿 by {data.get('author_name') or 'unknown'}", body=text,
                    author=str(data.get("author_name") or ""),
                )
            observed_statuses.append("empty_body")
        except Exception as exc:
            observed_statuses.append(self._status_from_exception(exc))

        # 明示設定されたBearerがある場合だけ公式APIを試す。tokenや失敗例外は結果へ含めない。
        bearer = os.getenv("X_BEARER_TOKEN") or os.getenv("TWITTER_BEARER_TOKEN")
        if bearer:
            try:
                response = await self._privacy_get(
                    client,
                    f"https://api.x.com/2/tweets/{tweet_id}",
                    params={
                        "tweet.fields": "created_at,author_id,conversation_id,in_reply_to_user_id,entities,attachments,referenced_tweets",
                        "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
                        "user.fields": "name,username",
                        "media.fields": "alt_text,type,url,preview_image_url",
                    },
                    headers={**self.headers, "Authorization": f"Bearer {bearer}"},
                    action="x.api.tweet.get",
                    source_kind="x_api",
                )
                if bool(getattr(response, "is_redirect", False)) or int(
                    getattr(response, "status_code", 200) or 200
                ) in range(300, 400):
                    observed_statuses.append("access_denied")
                    raise RuntimeError("redirect rejected")
                response.raise_for_status()
                payload = response.json()
                api_result = await self._x_api_result(
                    client,
                    url=url,
                    tweet_id=tweet_id,
                    payload=payload,
                    bearer=bearer,
                )
                if api_result is not None:
                    return api_result
                observed_statuses.append(self._x_payload_status(payload))
            except Exception as exc:
                observed_statuses.append(self._status_from_exception(exc))

        # Cookieは取得層だけで使う。認証済み呼び出しでは所有ユーザーの個人
        # credential/tombstone を先に解決し、行が存在しない場合だけ明示設定の
        # operator-managed shared fallback を許可する。
        if x_cookie is not None:
            resolution = x_cookie
            cookies = dict(resolution.cookies) if resolution.configured else {}
            csrf_token = cookies.get("ct0", "")
        elif session is not None and user_id is not None:
            resolution = await resolve_x_cookie(session, user_id)
            cookies = dict(resolution.cookies)
            csrf_token = cookies.get("ct0", "")
        else:
            cookies, csrf_token = self._load_x_cookie_secret()
        if cookies:
            try:
                response = await self._privacy_get(
                    client,
                    url,
                    cookies=cookies,
                    headers={
                        **self.headers,
                        "x-csrf-token": csrf_token,
                        "x-twitter-auth-type": "OAuth2Session",
                        "x-twitter-active-user": "yes",
                    },
                    action="x.authenticated_html.get",
                    source_kind="x_authenticated_html",
                )
                if bool(getattr(response, "is_redirect", False)) or int(
                    getattr(response, "status_code", 200) or 200
                ) in range(300, 400):
                    observed_statuses.append("access_denied")
                    response = None
                if response is None:
                    raise RuntimeError("redirect rejected")
                response.raise_for_status()
                cookie_result = self._x_authenticated_html_result(url, response.text)
                if cookie_result is not None:
                    return cookie_result
                observed_statuses.append(self._x_html_status(response.text))
            except Exception as exc:
                observed_statuses.append(self._status_from_exception(exc))

        final_status = self._most_specific_x_status(observed_statuses)
        return UrlFetchResult(
            requested_url=url,
            final_url=url,
            provider="x",
            error=self._status_message(final_status),
            acquisition_status=final_status,
        )

    async def _x_api_result(
        self,
        client: httpx.AsyncClient,
        *,
        url: str,
        tweet_id: str,
        payload: dict[str, Any],
        bearer: str,
    ) -> UrlFetchResult | None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        text = str(data.get("text") or "").strip()
        if not text:
            return None
        includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
        users = includes.get("users") if isinstance(includes.get("users"), list) else []
        media = includes.get("media") if isinstance(includes.get("media"), list) else []
        author = users[0] if users and isinstance(users[0], dict) else {}
        entities = data.get("entities") if isinstance(data.get("entities"), dict) else {}
        thread_context: list[str] = []
        conversation_id = str(data.get("conversation_id") or "")
        username = str(author.get("username") or "")
        if conversation_id and username:
            try:
                thread_response = await self._privacy_get(
                    client,
                    "https://api.x.com/2/tweets/search/recent",
                    params={
                        "query": f"conversation_id:{conversation_id} from:{username}",
                        "max_results": "20",
                        "tweet.fields": "created_at,referenced_tweets",
                    },
                    headers={**self.headers, "Authorization": f"Bearer {bearer}"},
                    action="x.api.thread.search",
                    source_kind="x_api",
                )
                if not bool(getattr(thread_response, "is_redirect", False)) and int(
                    getattr(thread_response, "status_code", 200) or 200
                ) not in range(300, 400) and thread_response.is_success:
                    thread_payload = thread_response.json()
                    thread_context = [
                        str(tweet.get("text") or "")
                        for tweet in thread_payload.get("data", [])
                        if isinstance(tweet, dict)
                        and str(tweet.get("id")) != tweet_id
                        and tweet.get("text")
                    ]
            except Exception:
                thread_context = []
        return UrlFetchResult(
            requested_url=url,
            final_url=url,
            success=True,
            provider="x_api_v2",
            title=f"X投稿 by {author.get('name') or author.get('username') or 'unknown'}",
            body=text,
            author=str(author.get("name") or author.get("username") or ""),
            published_at=str(data.get("created_at") or ""),
            quoted_post="\n".join(
                str(item.get("text") or "")
                for item in includes.get("tweets", [])
                if isinstance(item, dict)
            ),
            thread_context=thread_context,
            external_links=[
                str(item.get("expanded_url"))
                for item in entities.get("urls", [])
                if isinstance(item, dict) and item.get("expanded_url")
            ],
            media_descriptions=[
                str(item.get("alt_text") or item.get("type") or "")
                for item in media
                if isinstance(item, dict)
            ],
        )

    @staticmethod
    def _x_payload_status(payload: Any) -> str:
        text = json.dumps(payload, ensure_ascii=False).casefold() if isinstance(payload, (dict, list)) else str(payload).casefold()
        if any(token in text for token in ("age-restricted", "age restricted", "sensitive content", "possibly_sensitive", "センシティブ", "年齢制限")):
            return "restricted"
        if any(token in text for token in ("protected", "private account", "非公開")):
            return "private"
        if any(token in text for token in ("login", "authorization", "authenticate", "認証")):
            return "auth_required"
        if any(token in text for token in ("not found", "deleted", "does not exist", "削除")):
            return "deleted"
        return "empty_body"

    @staticmethod
    def _x_html_status(value: str) -> str:
        text = str(value or "").casefold()
        if any(token in text for token in ("age-restricted", "age restricted", "sensitive content", "sensitive media", "センシティブ", "年齢制限")):
            return "restricted"
        if any(token in text for token in ("this account’s posts are protected", "these posts are protected", "非公開")):
            return "private"
        if any(token in text for token in ("log in to x", "sign in to x", "ログイン")):
            return "auth_required"
        if any(token in text for token in ("this post is unavailable", "doesn’t exist", "deleted", "削除")):
            return "deleted"
        return "empty_body"

    @classmethod
    def _most_specific_x_status(cls, statuses: list[str]) -> str:
        priority = (
            "private",
            "restricted",
            "auth_required",
            "access_denied",
            "rate_limited",
            "deleted",
            "network_error",
            "empty_body",
            "fetch_failed",
        )
        return next((status for status in priority if status in statuses), "fetch_failed")

    @staticmethod
    def _load_x_cookie_secret() -> tuple[dict[str, str], str]:
        resolution = load_global_x_cookie()
        if not resolution.configured:
            return {}, ""
        cookies = dict(resolution.cookies)
        return cookies, cookies.get("ct0", "")

    @classmethod
    def _x_authenticated_html_result(
        cls,
        url: str,
        value: str,
    ) -> UrlFetchResult | None:
        soup = BeautifulSoup(str(value or ""), "html.parser")
        description = ""
        for attr, key in (("property", "og:description"), ("name", "twitter:description")):
            node = soup.find("meta", attrs={attr: key})
            candidate = str(node.get("content") or "").strip() if node else ""
            if candidate:
                description = candidate
                break
        if not description or cls._x_html_status(description) != "empty_body":
            return None
        title_node = soup.find("meta", attrs={"property": "og:title"})
        title = str(title_node.get("content") or "").strip() if title_node else "X投稿"
        return UrlFetchResult(
            requested_url=url,
            final_url=url,
            success=True,
            provider="x_authenticated_html",
            title=title or "X投稿",
            body=description,
        )

    @staticmethod
    def _meta(soup: BeautifulSoup, attr: str, value: str) -> str:
        node = soup.find("meta", attrs={attr: value})
        return str(node.get("content") or "").strip() if node else ""

    @staticmethod
    def _citation_doi_url(value: str) -> str:
        """Normalize one citation DOI metadata value to a public DOI URL."""

        text = str(value or "").strip()
        if text.casefold().startswith("doi:"):
            text = text[4:].strip()
        try:
            parts = urlsplit(text)
        except ValueError:
            return ""
        if parts.scheme in {"http", "https"} and (parts.hostname or "").casefold() in {
            "doi.org",
            "dx.doi.org",
            "www.doi.org",
        }:
            text = parts.path.lstrip("/")
        if not re.fullmatch(r"10\.\d{4,9}/\S+", text, flags=re.IGNORECASE):
            return ""
        return "https://doi.org/" + quote(text, safe="/._-()")

    @staticmethod
    def _external_links(soup: BeautifulSoup, base_url: str) -> list[str]:
        base_host = (urlsplit(base_url).hostname or "").casefold()
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            link = urljoin(base_url, str(anchor["href"]))
            if urlsplit(link).scheme in {"http", "https"} and (urlsplit(link).hostname or "").casefold() != base_host:
                if link not in links:
                    links.append(link)
        return links[:100]

    @staticmethod
    def _public_link_hints(soup: BeautifulSoup, base_url: str) -> list[str]:
        """Return bounded same-site author/reference links from article markup.

        Ordinary external-link extraction intentionally excludes same-host
        navigation.  Citation expansion needs only explicit author/reference
        hints, so retain those rel/class/path signals while ignoring generic
        menus and links embedded in script text.
        """

        base_host = (urlsplit(base_url).hostname or "").casefold()
        hints: list[str] = []
        path_markers = ("/author", "/authors", "/reference", "/references", "/citation", "/bibliograph")
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            if not href:
                continue
            link = urljoin(base_url, href)
            parts = urlsplit(link)
            if parts.scheme not in {"http", "https"}:
                continue
            if (parts.hostname or "").casefold() != base_host:
                continue
            rel = " ".join(str(item) for item in (anchor.get("rel") or [])).casefold()
            classes = " ".join(str(item) for item in (anchor.get("class") or [])).casefold()
            text = anchor.get_text(" ", strip=True).casefold()
            path = parts.path.casefold()
            if (
                "author" in rel
                or "citation" in rel
                or "reference" in rel
                or "author" in classes
                or "byline" in classes
                or any(marker in path for marker in path_markers)
                or text in {"author", "authors", "references", "citations"}
            ):
                normalized = parts._replace(fragment="").geturl()
                if normalized not in hints:
                    hints.append(normalized)
                if len(hints) >= 32:
                    break
        return hints
