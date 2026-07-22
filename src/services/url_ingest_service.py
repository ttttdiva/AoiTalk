"""再利用可能なURL直接取得サービス。

検索結果の推測と、元URLから取得できた事実を混同しないため、URLごとに
直接取得結果を独立して返す。X は oEmbed と公開 syndication API を順に試す。
"""

from __future__ import annotations

import asyncio
import html
import http.client
import ipaddress
import os
import re
import socket
import ssl
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup


_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
_GITHUB_HOSTS = {"github.com", "www.github.com"}


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
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UrlIngestService:
    def __init__(self, *, timeout_seconds: float = 25.0, max_body_chars: int = 60_000):
        self.timeout = httpx.Timeout(timeout_seconds, connect=8.0)
        self.max_body_chars = max_body_chars
        self.headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AoiTalkClip/1.0; +https://github.com/)",
            "Accept-Language": "ja,en;q=0.8",
        }

    async def fetch_all(self, urls: list[str]) -> list[UrlFetchResult]:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=self.headers
        ) as client:
            return await asyncio.gather(*(self._fetch_one(client, url) for url in urls))

    async def _fetch_one(self, client: httpx.AsyncClient, url: str) -> UrlFetchResult:
        host = urlsplit(url).hostname or ""
        try:
            await self._assert_public_url(url)
            if host.casefold() in _X_HOSTS:
                return await self._fetch_x(client, url)
            if host.casefold() in _GITHUB_HOSTS:
                github = await self._fetch_github(client, url)
                if github.success:
                    return github
            return await self._fetch_html(client, url)
        except (httpx.HTTPError, ValueError) as exc:
            return UrlFetchResult(requested_url=url, final_url=url, error=str(exc))
        except Exception as exc:  # URL単位で隔離し、全件の状態を必ず返す
            return UrlFetchResult(requested_url=url, final_url=url, error=f"予期しない取得エラー: {exc}")

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

    async def _fetch_github(self, client: httpx.AsyncClient, url: str) -> UrlFetchResult:
        parts = [part for part in urlsplit(url).path.split("/") if part]
        if len(parts) < 2:
            return await self._fetch_html(client, url)
        owner, repo = parts[0], parts[1].removesuffix(".git")
        api = await client.get(f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}")
        api.raise_for_status()
        meta = api.json()
        readme_text = ""
        readme = await client.get(
            f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/readme",
            headers={**self.headers, "Accept": "application/vnd.github.raw+json"},
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

    @staticmethod
    async def _resolve_public_url(url: str) -> tuple[Any, str]:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("http/httpsの公開URLではありません")
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
    def _pinned_request(parts, address: str) -> httpx.Response:
        host = str(parts.hostname)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        if parts.scheme == "https":
            class PinnedHttpsConnection(http.client.HTTPSConnection):
                def connect(self):
                    sock = socket.create_connection((address, port), timeout=25)
                    self.sock = self._context.wrap_socket(sock, server_hostname=host)
            connection = PinnedHttpsConnection(host, port=port, timeout=25, context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(address, port=port, timeout=25)
        try:
            connection.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", host if parts.port is None else f"{host}:{parts.port}")
            connection.putheader("User-Agent", "Mozilla/5.0 (compatible; AoiTalkClip/1.0)")
            connection.putheader("Accept-Language", "ja,en;q=0.8")
            connection.putheader("Accept-Encoding", "identity")
            connection.endheaders()
            raw = connection.getresponse()
            content = raw.read(8 * 1024 * 1024 + 1)
            if len(content) > 8 * 1024 * 1024:
                raise ValueError("取得本文がサイズ上限を超えました")
            url = parts.geturl()
            return httpx.Response(
                raw.status, headers=dict(raw.getheaders()), content=content,
                request=httpx.Request("GET", url),
            )
        finally:
            connection.close()

    async def _safe_get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """各redirect先を接続前に検証し、SSRFを防ぐ。"""
        current = url
        for _ in range(8):
            parts, address = await self._resolve_public_url(current)
            response = await asyncio.to_thread(self._pinned_request, parts, address)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return response
                current = urljoin(current, location)
                continue
            return response
        raise ValueError("リダイレクト回数が上限を超えました")

    async def _fetch_x(self, client: httpx.AsyncClient, url: str) -> UrlFetchResult:
        status = re.search(r"/(?:status|statuses)/(\d+)", urlsplit(url).path)
        if not status:
            raise ValueError("X投稿IDをURLから判別できません")
        tweet_id = status.group(1)
        errors: list[str] = []
        bearer = os.getenv("X_BEARER_TOKEN") or os.getenv("TWITTER_BEARER_TOKEN")
        if bearer:
            try:
                response = await client.get(
                    f"https://api.x.com/2/tweets/{tweet_id}",
                    params={
                        "tweet.fields": "created_at,author_id,conversation_id,in_reply_to_user_id,entities,attachments,referenced_tweets",
                        "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
                        "user.fields": "name,username",
                        "media.fields": "alt_text,type,url,preview_image_url",
                    },
                    headers={**self.headers, "Authorization": f"Bearer {bearer}"},
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                text = str(data.get("text") or "").strip()
                if text:
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
                            thread_response = await client.get(
                                "https://api.x.com/2/tweets/search/recent",
                                params={
                                    "query": f"conversation_id:{conversation_id} from:{username}",
                                    "max_results": "20",
                                    "tweet.fields": "created_at,referenced_tweets",
                                },
                                headers={**self.headers, "Authorization": f"Bearer {bearer}"},
                            )
                            if thread_response.is_success:
                                thread_payload = thread_response.json()
                                thread_context = [
                                    str(tweet.get("text") or "")
                                    for tweet in thread_payload.get("data", [])
                                    if isinstance(tweet, dict) and str(tweet.get("id")) != tweet_id and tweet.get("text")
                                ]
                        except Exception:
                            thread_context = []
                    return UrlFetchResult(
                        requested_url=url, final_url=url, success=True, provider="x_api_v2",
                        title=f"X投稿 by {author.get('name') or author.get('username') or 'unknown'}",
                        body=text, author=str(author.get("name") or author.get("username") or ""),
                        published_at=str(data.get("created_at") or ""),
                        quoted_post="\n".join(str(item.get("text") or "") for item in includes.get("tweets", []) if isinstance(item, dict)),
                        thread_context=thread_context,
                        external_links=[str(item.get("expanded_url")) for item in entities.get("urls", []) if isinstance(item, dict) and item.get("expanded_url")],
                        media_descriptions=[str(item.get("alt_text") or item.get("type") or "") for item in media if isinstance(item, dict)],
                    )
                errors.append("認証済みAPI応答に投稿本文がありません")
            except Exception as exc:
                errors.append(f"認証済みAPI: {exc}")
        # 公開syndicationは本文・投稿者・日時・引用・mediaを構造化して返す。
        try:
            response = await client.get(
                "https://cdn.syndication.twimg.com/tweet-result",
                params={"id": tweet_id, "lang": "ja"},
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
            errors.append("syndication応答に投稿本文がありません")
        except Exception as exc:
            errors.append(f"syndication: {exc}")
        # oEmbedは認証不要の別経路。本文を含むblockquoteだけを成功とする。
        try:
            response = await client.get(
                "https://publish.twitter.com/oembed", params={"url": url, "omit_script": "true"}
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
            errors.append("oEmbed応答に投稿本文がありません")
        except Exception as exc:
            errors.append(f"oEmbed: {exc}")
        return UrlFetchResult(
            requested_url=url, final_url=url, provider="x", error="; ".join(errors)
        )

    @staticmethod
    def _meta(soup: BeautifulSoup, attr: str, value: str) -> str:
        node = soup.find("meta", attrs={attr: value})
        return str(node.get("content") or "").strip() if node else ""

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
