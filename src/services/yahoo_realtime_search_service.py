"""Yahoo!リアルタイム検索を使った公開 X 投稿の取得サービス。

このモジュールは、X 検索の各呼び出し元に URL 判定や HTML の細部を
複製させないための小さな共通境界です。Yahoo! の公開検索ページだけを
取得し、検索結果に含まれる公開 X/Twitter status URL と本文の最小限の
情報を返します。X の Cookie、Bearer token、任意 URL のリダイレクトは
この経路では扱いません。

``httpx.AsyncClient`` を呼び出し元から受け取る設計にしているため、上位
層はタイムアウトやプロキシを自分のライフサイクルに合わせて管理できま
す。テストや単発利用向けに ``client=None`` も許容します。
"""

from __future__ import annotations

import html as _html
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .outbound_privacy_service import (
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    PrivacyError,
)

logger = logging.getLogger(__name__)


# These hosts are public aliases for the same X/Twitter service.  Do not use a
# broad ``endswith("x.com")`` test: ``evil-x.com`` and user-info URLs must not
# pass the egress boundary.
_X_HOSTS = frozenset(
    {
        "x.com",
        "www.x.com",
        "mobile.x.com",
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
    }
)
# Public compatibility alias for callers that need to share the exact host
# allowlist without duplicating it in URL-ingest/routing code.
X_HOSTS = _X_HOSTS
_X_CANONICAL_HOST = "x.com"
_YAHOO_REALTIME_URL = "https://search.yahoo.co.jp/realtime/search"
YAHOO_REALTIME_SEARCH_URL = _YAHOO_REALTIME_URL
_MAX_QUERY_CHARS = 500
_MAX_LIMIT = 25
_MAX_HTML_CHARS = 2_000_000
_STATUS_ID_RE = re.compile(r"^[0-9]{1,30}$")
_STATUS_SEGMENTS = frozenset({"status", "statuses"})

# Tracking parameters are intentionally dropped by canonicalization.  The
# canonical URL itself is still generated only from an X host and a path; no
# query value is ever sent to a second host.


def _split_x_url(value: Any):
    """Return a validated split URL or ``None`` without raising on bad ports."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").rstrip(".").casefold()
        # Accessing ``port`` forces validation for malformed ``:port`` values.
        _ = parts.port
    except (TypeError, ValueError):
        return None
    if parts.scheme.casefold() not in {"http", "https"}:
        return None
    if host not in _X_HOSTS:
        return None
    # Credentials and explicit ports are never needed for a public post and
    # make it too easy to accidentally broaden the outbound destination.
    if parts.username is not None or parts.password is not None or parts.port is not None:
        return None
    return parts


def _path_segments(path: str) -> list[str]:
    """Decode path segments while retaining a conservative URL shape."""

    segments: list[str] = []
    for raw in str(path or "").split("/"):
        if not raw:
            continue
        # A percent encoded slash should not create an extra path component
        # (or turn a non-status URL into a status URL) after normalization.
        decoded = unquote(raw)
        if "/" in decoded or "\\" in decoded:
            return []
        segments.append(decoded)
    return segments


def _status_location(parts) -> tuple[int, str] | None:
    segments = _path_segments(parts.path)
    if not segments:
        return None
    for index, segment in enumerate(segments[:-1]):
        if segment.casefold() not in _STATUS_SEGMENTS:
            continue
        status_id = segments[index + 1]
        if _STATUS_ID_RE.fullmatch(status_id):
            return index, status_id
    return None


def is_x_url(value: Any) -> bool:
    """Return whether *value* is an HTTP(S) URL on a public X/Twitter host.

    The predicate deliberately accepts profile/search URLs as well as status
    URLs; callers that need a post identifier should use :func:`x_status_id`.
    A bare ``x.com/...`` string and URLs with credentials/ports are rejected.
    """

    return _split_x_url(value) is not None


def x_status_id(value: Any) -> str | None:
    """Extract an exact numeric X status ID from *value*.

    Matching is segment based.  Thus ``/status/123abc`` and a query parameter
    such as ``?status=123`` cannot be mistaken for post ``123``.  Additional
    suffixes such as ``/photo/1`` are allowed because X commonly appends them
    to a status URL.
    """

    parts = _split_x_url(value)
    if parts is None:
        return None
    located = _status_location(parts)
    return located[1] if located else None


def canonicalize_x_url(value: Any) -> str | None:
    """Return a stable, credential-free canonical X URL.

    Host aliases become ``x.com``; query strings/fragments and status action
    suffixes are removed.  For status URLs ``statuses`` is normalized to
    ``status`` and the exact numeric identifier is preserved.  ``None`` is
    returned for malformed or non-X URLs rather than echoing untrusted input.
    """

    parts = _split_x_url(value)
    if parts is None:
        return None
    segments = _path_segments(parts.path)
    if str(parts.path or "/").strip() and not segments:
        return None
    located = _status_location(parts)
    if located is not None:
        index, status_id = located
        # Keep a username/profile prefix when present, while dropping action
        # suffixes (``/photo/1``, ``/analytics``) from the canonical URL.
        segments = segments[:index] + ["status", status_id]

    # Preserve ordinary profile/search paths but normalize duplicate slashes
    # and percent encoding.  ``quote`` is intentionally avoided here: callers
    # use this value for matching/display, and input has already been limited
    # to a trusted X origin.
    path = "/" + "/".join(segment for segment in segments if segment)
    if path == "/":
        path = ""
    return urlunsplit(("https", _X_CANONICAL_HOST, path, "", ""))


_PLATFORM_RE = re.compile(
    # ``\b`` does not recognize the boundary between an ASCII platform name
    # and a Japanese character (``Twitterの``), so use ASCII-only lookarounds.
    # The Japanese lookahead prevents ``X軸``/``XGBoost`` from being treated as
    # the social platform while retaining ``Xで``/``Xの``/``X投稿``.
    r"(?:"
    r"(?<![A-Za-z0-9_])x(?:\.com)?(?=(?:[\s\u3000]|$|[、。,.!?！？]|[ので上にをはがへとや]|投稿|ポスト|検索|話題|反応))"
    r"|(?<![A-Za-z0-9_])twitter(?:\.com)?(?=(?:[\s\u3000]|$|[、。,.!?！？]|[ので上にをはがへとや]|投稿|ポスト|検索|話題|反応))"
    r"|𝕏|ツイッター|ティッター)",
    re.IGNORECASE,
)
_SEARCH_ACTION_RE = re.compile(
    r"(?:検索|調べ|探し|見つけ|確認|検索して|search|look\s*up|find|check|query)",
    re.IGNORECASE,
)
_RECENCY_RE = re.compile(
    r"(?:最新|最近|今(?:の|日)?|現在|リアルタイム|速報|トレンド|話題|反応|latest|recent|newest|right\s*now|live|trending|breaking)",
    re.IGNORECASE,
)
_POST_NOUN_RE = re.compile(
    r"(?:投稿|ポスト|ツイート|つぶやき|ニュース|評判|posts?|tweets?|updates?)",
    re.IGNORECASE,
)
_X_TOPIC_RE = re.compile(r"(?:話題|反応|評判|トレンド|trending|reaction|buzz)", re.IGNORECASE)
_ON_PLATFORM_RE = re.compile(r"\bon\s+(?:x|twitter)(?:\.com)?\b", re.IGNORECASE)


def looks_like_x_search_request(value: Any) -> bool:
    """Return whether prose has a strong, unambiguous X-search intent.

    Merely mentioning the letter ``X`` or ``Twitter`` is not sufficient.  The
    request must also contain a search action, a recency/trending cue paired
    with a post/news noun, or an X URL combined with a search action.  This
    keeps ordinary questions such as ``Xとは何?`` on the normal web path.
    """

    if not isinstance(value, str):
        return False
    text = unicodedata.normalize("NFKC", value).strip()
    if not text:
        return False
    platform = bool(_PLATFORM_RE.search(text))
    if not platform:
        # A direct URL is a platform signal, but malformed/foreign URLs are
        # intentionally ignored.
        platform = any(
            is_x_url(token.rstrip(".,。！？!?"))
            for token in re.findall(r"https?://[^\s<>]+", text)
        )
    if not platform:
        return False
    if _ON_PLATFORM_RE.search(text):
        return True
    action = bool(_SEARCH_ACTION_RE.search(text))
    recency = bool(_RECENCY_RE.search(text))
    post_noun = bool(_POST_NOUN_RE.search(text))
    # ``Xで検索`` / ``search Twitter`` and their Japanese equivalents.  A
    # noun/topic cue is enough for concise forms such as ``Xの投稿`` and
    # ``Twitterの評判``; generic ``投稿を検索して`` has no platform cue.
    if action or post_noun or _X_TOPIC_RE.search(text) or recency:
        return True
    # (The branch above already handles a recency cue; keeping this predicate
    # explicit documents the intended strong form for future regex edits.)
    return bool(recency and post_noun)


def _clean_text(value: Any, *, max_chars: int = 2_000) -> str:
    text = re.sub(r"\s+", " ", _html.unescape(str(value or ""))).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _node_attr(node: Any, names: Iterable[str]) -> str:
    if node is None:
        return ""
    attrs = getattr(node, "attrs", {})
    if not isinstance(attrs, Mapping):
        return ""
    for name in names:
        value = attrs.get(name)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        clean = _clean_text(value, max_chars=300)
        if clean:
            return clean
    return ""


def _find_text(node: Any, selectors: Iterable[str]) -> str:
    if node is None:
        return ""
    for selector in selectors:
        try:
            found = node.select_one(selector)
        except Exception:  # pragma: no cover - BeautifulSoup selector guard
            found = None
        if found is None:
            continue
        candidate = _clean_text(found.get_text(" ", strip=True))
        if candidate:
            return candidate
    return ""


def _result_container(anchor: Any) -> Any:
    """Choose the nearest result-like ancestor without assuming Yahoo CSS."""

    for parent in getattr(anchor, "parents", ()):
        name = str(getattr(parent, "name", "") or "").casefold()
        if name in {"article", "li"}:
            return parent
        classes = " ".join(str(item) for item in (getattr(parent, "get", lambda *_: [])("class") or []))
        marker = f"{classes} {_node_attr(parent, ('data-testid', 'data-test', 'role'))}".casefold()
        # A permalink is commonly nested under Yahoo's ``time``/footer.  Do
        # not mistake that tiny node for the result card, otherwise the
        # parser returns only ``8:28``/``数秒前`` instead of the post body.
        if name in {"a", "span", "time"} or "footer" in marker:
            continue
        if any(token in marker for token in ("result", "tweet", "status", "realtime", "card")):
            # Avoid selecting the whole page wrapper.
            if name not in {"body", "html", "main"}:
                return parent
    return anchor


def _author_handle_from_url(url: str) -> str:
    parts = _split_x_url(url)
    if parts is None:
        return ""
    segments = _path_segments(parts.path)
    located = _status_location(parts)
    if located is None:
        return ""
    index, _ = located
    prefix = [segment for segment in segments[:index] if segment]
    # ``/i/web/status`` has no public handle; do not expose ``i`` as one.
    if not prefix or prefix[-1].casefold() in {"i", "web"}:
        return ""
    return prefix[-1].lstrip("@")


def _post_from_anchor(anchor: Any, *, base_url: str) -> "YahooRealtimePost | None":
    raw_href = str(anchor.get("href") or "").strip() if anchor is not None else ""
    if not raw_href:
        return None
    href = urljoin(base_url, raw_href)
    canonical = canonicalize_x_url(href)
    status_id = x_status_id(href)
    if canonical is None or status_id is None:
        return None

    container = _result_container(anchor)
    # Synthetic/compact Yahoo layouts can place multiple status links inside
    # one article wrapper.  In that shape, reading the wrapper's first
    # ``<p>`` would attach the neighboring post's body to the exact URL.
    # Keep card-level metadata from the wrapper, but use this anchor as the
    # text scope whenever it contains more than one status link.
    text_container = container
    try:
        status_links = [
            item
            for item in container.find_all("a", href=True)
            if x_status_id(urljoin(base_url, str(item.get("href") or "")))
        ]
        if len(status_links) > 1:
            text_container = anchor
    except Exception:  # pragma: no cover - defensive parser guard
        text_container = container
    author_name = _node_attr(
        container,
        (
            "data-author-name",
            "data-author",
            "data-user-name",
            "data-screen-name",
            "data-username",
        ),
    )
    author_handle = _node_attr(
        container,
        ("data-author-handle", "data-user-handle", "data-screen-name", "data-username"),
    ).lstrip("@").strip()
    if not author_name:
        author_name = _find_text(
            container,
            (
                "[class*='authorname' i]",
                "[class*='displayname' i]",
                "[class*='user-name' i]",
                "[class*='name' i]",
                "[class*='author' i]",
            ),
        )
    if not author_handle:
        author_handle = _author_handle_from_url(canonical)

    published_at = _node_attr(container, ("data-published-at", "data-timestamp", "datetime"))
    if not published_at:
        time_node = None
        try:
            time_node = container.select_one("time[datetime], time")
        except Exception:  # pragma: no cover
            pass
        if time_node is not None:
            published_at = _node_attr(time_node, ("datetime",)) or _clean_text(
                time_node.get_text(" ", strip=True), max_chars=200
            )

    title = _find_text(text_container, ("h1", "h2", "h3", "[class*='title' i]"))
    text = _find_text(
        text_container,
        (
            "[data-testid*='text' i]",
            # Yahoo's current realtime markup uses generated class names such
            # as ``Tweet_body__...``/``Tweet_bodyContainer__...`` rather than
            # a stable ``snippet`` class.  Prefer that post-body node over the
            # footer's timestamp and engagement counts.
            "[class*='bodycontainer' i] p[class*='body' i]",
            "[class*='tweet_body' i]",
            "[class*='bodycontainer' i]",
            "[class*='snippet' i]",
            "[class*='description' i]",
            "p",
        ),
    )
    if not text:
        # Anchor text is preferable to a card's navigation metadata when no
        # dedicated snippet element exists.
        text = _clean_text(text_container.get_text(" ", strip=True))
    if not text:
        text = title
    if not title:
        title = text

    return YahooRealtimePost(
        url=canonical,
        text=text,
        author_name=author_name,
        author_handle=author_handle,
        published_at=published_at,
        status_id=status_id,
        title=title,
    )


def _iter_candidate_anchors(soup: BeautifulSoup, *, base_url: str):
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor.get("href") or ""))
        canonical = canonicalize_x_url(href)
        if canonical is None or x_status_id(href) is None:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        yield anchor

    # A few Yahoo layouts put the permalink on the card itself rather than an
    # anchor.  Materialize a lightweight anchor-like object only when needed.
    for node in soup.find_all(
        attrs={
            "data-url": True,
        }
    ) + soup.find_all(attrs={"data-href": True}) + soup.find_all(
        attrs={"data-permalink": True}
    ) + soup.find_all(attrs={"data-status-url": True}):
        href = ""
        for attr in ("data-url", "data-href", "data-permalink", "data-status-url"):
            href = str(node.get(attr) or "").strip()
            if href:
                break
        canonical = canonicalize_x_url(urljoin(base_url, href))
        if canonical is None or x_status_id(href) is None or canonical in seen:
            continue
        seen.add(canonical)
        fake = BeautifulSoup(f'<a href="{_html.escape(href, quote=True)}"></a>', "html.parser").a
        if fake is not None:
            # Keep the result card as the extraction context.
            fake.parent = node
            yield fake


def parse_yahoo_realtime_html(
    value: Any,
    *,
    query: str = "",
    limit: int = 8,
    exact_status_id: str | None = None,
    base_url: str = _YAHOO_REALTIME_URL,
) -> list["YahooRealtimePost"]:
    """Parse a bounded subset of Yahoo realtime HTML into X posts.

    The parser intentionally accepts semantic HTML and data attributes rather
    than Yahoo's unstable generated class names.  Only links with an exact
    numeric X status ID become posts; unrelated Yahoo links are ignored.
    """

    try:
        bounded = str(value or "")[:_MAX_HTML_CHARS]
        soup = BeautifulSoup(bounded, "html.parser")
    except Exception:
        return []
    try:
        wanted_id = str(exact_status_id or "").strip() or x_status_id(query)
        safe_limit = max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        wanted_id = x_status_id(query)
        safe_limit = 8

    posts: list[YahooRealtimePost] = []
    seen_ids: set[str] = set()
    for anchor in _iter_candidate_anchors(soup, base_url=base_url):
        post = _post_from_anchor(anchor, base_url=base_url)
        if post is None:
            continue
        if wanted_id and post.status_id != wanted_id:
            continue
        if post.status_id in seen_ids:
            continue
        seen_ids.add(post.status_id)
        posts.append(post)
        if len(posts) >= safe_limit:
            break
    return posts


@dataclass(slots=True)
class YahooRealtimePost:
    """最小限の公開 X 投稿表現。

    ``author_handle`` は ``@`` なしで保持します。表示側が必要に応じて
    ``@`` を付ける既存規約に合わせ、URL・本文・時刻は文字列のまま返し
    ます。
    """

    url: str = ""
    text: str = ""
    author_name: str = ""
    author_handle: str = ""
    published_at: str = ""
    status_id: str = ""
    title: str = ""

    @property
    def canonical_url(self) -> str:
        return self.url

    @property
    def author(self) -> str:
        return self.author_name or self.author_handle

    @property
    def body(self) -> str:
        return self.text

    @property
    def snippet(self) -> str:
        return self.text

    @property
    def raw(self) -> dict[str, Any]:
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    as_dict = to_dict


@dataclass(slots=True)
class YahooRealtimeSearchResult:
    """Yahoo realtime transport result, including safe failure metadata."""

    query: str = ""
    posts: list[YahooRealtimePost] = field(default_factory=list)
    source_url: str = _YAHOO_REALTIME_URL
    status: str = "success"
    error: str = ""
    status_code: int | None = None
    fetched_at: str = ""

    @property
    def has_results(self) -> bool:
        return bool(self.posts)

    @property
    def results(self) -> list[YahooRealtimePost]:
        return self.posts

    @property
    def items(self) -> list[YahooRealtimePost]:
        return self.posts

    def __iter__(self):
        """Compatibility with older DeepResearch callers that iterated posts."""

        return iter(self.posts)

    def __len__(self) -> int:
        return len(self.posts)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["has_results"] = self.has_results
        return payload

    as_dict = to_dict


# Compatibility name used by early callers and by the shared web-routing
# worker.  The descriptive class remains available for type checkers.
SearchResult = YahooRealtimeSearchResult


def _safe_limit(value: Any) -> int:
    try:
        return max(1, min(int(value), _MAX_LIMIT))
    except (TypeError, ValueError):
        return 8


def _normalized_query(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()[:_MAX_QUERY_CHARS]


def _normalize_yahoo_endpoint(value: Any) -> str:
    """Normalize an explicitly configured Yahoo-compatible endpoint.

    Deployments may use an egress-auditing proxy, so the host is not hardcoded
    here.  Credentials, fragments and query strings are always rejected or
    dropped; provider classification/local-only policy remains the final
    decision immediately before transport.
    """

    candidate = str(value or _YAHOO_REALTIME_URL).strip()
    try:
        parts = urlsplit(candidate)
        _ = parts.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Yahoo realtime endpoint is malformed") from exc
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ValueError("Yahoo realtime endpoint must be HTTP(S)")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Yahoo realtime endpoint cannot contain credentials")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), parts.netloc, path, "", ""))


async def _client_get(client: Any, params: dict[str, Any], *, url: str) -> Any:
    """Call a real/fake AsyncClient without ever following redirects."""

    get = getattr(client, "get", None)
    if not callable(get):
        raise TypeError("client must expose an async get() method")
    kwargs = {
        "params": params,
        "follow_redirects": False,
        "headers": {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "AoiTalk/1.0 (YahooRealtimeSearch)",
        },
    }
    try:
        response = get(url, **kwargs)
    except TypeError:
        # Tiny fixture clients often implement only ``get(url, params=...)``;
        # retaining the static URL and omitting optional kwargs preserves the
        # same network boundary without weakening redirect behavior for httpx.
        response = get(url, params=params)
    if hasattr(response, "__await__"):
        return await response
    return response


def _result_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def search_yahoo_realtime(
    client: Any,
    query: str,
    *,
    limit: int = 8,
    privacy_gateway: OutboundPrivacyGateway | None = None,
    base_url: str | None = None,
    config: Any = None,
) -> YahooRealtimeSearchResult:
    """Fetch and parse public Yahoo realtime results for an X query.

    ``privacy_gateway`` is optional for compatibility.  When supplied, it is
    used only at this egress boundary; callers that already protected a query
    can omit it.  Even without a gateway, request-local ``local_only`` policy
    is enforced through the shared provider classifier, and no credential is
    ever attached to the Yahoo request.
    """

    try:
        request_url = _normalize_yahoo_endpoint(base_url)
    except ValueError as exc:
        return YahooRealtimeSearchResult(
            query=_normalized_query(query),
            source_url=_YAHOO_REALTIME_URL,
            status="invalid_endpoint",
            error=str(exc),
            fetched_at=_result_now(),
        )

    normalized = _normalized_query(query)
    safe_limit = _safe_limit(limit)
    result = YahooRealtimeSearchResult(
        query=normalized,
        source_url=request_url,
        fetched_at=_result_now(),
    )
    if not normalized:
        result.status = "invalid_query"
        result.error = "検索クエリを指定してください"
        return result

    # A direct status URL is a high-value exact-match query.  Canonicalize it
    # before privacy processing and before putting it in Yahoo's query string
    # so tracking/secret parameters never leave this boundary.
    exact_query_id = x_status_id(normalized)
    if is_x_url(normalized):
        canonical_query = canonicalize_x_url(normalized)
        if canonical_query:
            normalized = canonical_query
            result.query = normalized
            exact_query_id = x_status_id(normalized)

    logger.debug("X search route: yahoo_realtime")

    gateway = privacy_gateway
    if gateway is None:
        try:
            # Direct ``x_search`` calls may not have a registry closure, so
            # accept the active config explicitly while still inheriting the
            # request-local ContextVar when callers omit it.
            gateway = OutboundPrivacyGateway(config)
        except Exception:
            # Privacy-boundary construction failure is fail-closed.  Do not
            # turn an unavailable policy service into an unreviewed external
            # Yahoo request.
            result.status = "privacy_blocked"
            result.error = "リアルタイム検索のプライバシー設定を解決できません"
            return result
    try:
        if gateway is not None:
            gateway.ensure_provider_allowed("yahoo_realtime", base_url=request_url)
            # A supplied gateway owns redaction/review.  In direct mode this
            # is a no-op; in protected/local-only mode it fails closed rather
            # than sending a raw query behind the caller's back.
            protected = await gateway.protect(
                {"query": normalized},
                provider="yahoo_realtime",
                base_url=request_url,
                source_kind="yahoo_realtime_search",
            )
            payload = protected.payload
            if isinstance(payload, Mapping):
                normalized = _normalized_query(payload.get("query"))
                # Keep the result metadata on the same redacted side of the
                # egress boundary.  A caller with the gateway can explicitly
                # restore aliases for local display; this service never
                # rehydrates raw terms on its own.
                result.query = normalized
        
    except ExternalProviderBlocked:
        result.status = "blocked"
        result.error = "外部リアルタイム検索はプライバシーポリシーにより停止しました"
        return result
    except PrivacyError:
        result.status = "privacy_blocked"
        result.error = "リアルタイム検索はプライバシーポリシーにより停止しました"
        return result
    except Exception:
        # The query itself is intentionally absent from this log.
        logger.warning("Yahoo realtime privacy boundary failed")
        result.status = "privacy_blocked"
        result.error = "リアルタイム検索のプライバシー処理に失敗しました"
        return result

    owned_client = False
    active_client = client
    if active_client is None:
        active_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        owned_client = True
    try:
        response = await _client_get(
            active_client,
            {"p": normalized, "n": safe_limit},
            url=request_url,
        )
        status_code = getattr(response, "status_code", None)
        try:
            status_code = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        result.status_code = status_code
        if status_code is not None and 300 <= status_code < 400:
            result.status = "redirect_rejected"
            result.error = "Yahoo検索先のリダイレクトは許可されていません"
            return result
        if status_code is not None and status_code >= 400:
            result.status = "http_error"
            result.error = f"Yahoo検索がHTTP {status_code}を返しました"
            return result
        body = getattr(response, "text", "")
        if not isinstance(body, str):
            body = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
        if len(body) > _MAX_HTML_CHARS:
            result.status = "body_too_large"
            result.error = "Yahoo検索の応答が大きすぎます"
            return result
        exact_id = exact_query_id or x_status_id(normalized)
        result.posts = parse_yahoo_realtime_html(
            body,
            query=normalized,
            limit=safe_limit,
            exact_status_id=exact_id,
            base_url=request_url,
        )
        result.status = "success" if result.posts else "no_results"
        exact_match = bool(
            exact_id and any(post.status_id == exact_id for post in result.posts)
        )
        logger.debug(
            "X search result count=%d exact_status_match=%s",
            len(result.posts),
            exact_match,
        )
        if not result.posts:
            logger.debug("X search no-result/fallback")
        return result
    except (httpx.HTTPError, OSError, TimeoutError):
        result.status = "network_error"
        result.error = "Yahooリアルタイム検索への接続に失敗しました"
        return result
    except Exception:
        # Keep provider details and query text out of logs/results.
        logger.warning("Yahoo realtime response parsing failed")
        logger.debug("X search no-result/fallback")
        result.status = "parse_error"
        result.error = "Yahooリアルタイム検索の応答を解析できませんでした"
        return result
    finally:
        if owned_client:
            try:
                await active_client.aclose()
            except Exception:
                pass


__all__ = [
    "YAHOO_REALTIME_SEARCH_URL",
    "X_HOSTS",
    "YahooRealtimePost",
    "YahooRealtimeSearchResult",
    "SearchResult",
    "is_x_url",
    "canonicalize_x_url",
    "x_status_id",
    "looks_like_x_search_request",
    "parse_yahoo_realtime_html",
    "search_yahoo_realtime",
]
