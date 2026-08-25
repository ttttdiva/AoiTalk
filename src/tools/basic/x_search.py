"""Yahoo-backed X (formerly Twitter) search tool.

The Yahoo realtime search service is the canonical transport for X lookups in
normal chat.  This module intentionally contains only the synchronous tool
bridge and result presentation; URL parsing and the Yahoo response parser live
in :mod:`src.services.yahoo_realtime_search_service`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
from collections.abc import Mapping
from typing import Any

from ..core import tool


def _service_function(name: str):
    """Resolve a Yahoo service symbol lazily.

    Keeping this import lazy makes the basic-tool package importable while the
    optional service is being installed and also gives tests a narrow patch
    seam.  The service itself remains the sole owner of Yahoo HTML/API
    parsing.
    """

    from ...services import yahoo_realtime_search_service as service

    return getattr(service, name)


def is_x_url(value: str) -> bool:
    """Return whether *value* is a direct X/Twitter status URL."""

    return bool(_service_function("is_x_url")(value))


def looks_like_x_search_request(value: str) -> bool:
    """Return whether prose contains an unambiguous X-search intent."""

    return bool(_service_function("looks_like_x_search_request")(value))


async def search_yahoo_realtime(
    client: Any,
    query: str,
    *,
    limit: int = 8,
    privacy_gateway: Any = None,
    config: Any = None,
):
    """Delegate to the canonical Yahoo realtime service.

    ``client`` is retained as the first argument to match the shared service
    contract.  Passing ``None`` asks the service to create its own short-lived
    HTTP client.
    """

    implementation = _service_function("search_yahoo_realtime")
    kwargs = {"limit": limit}
    if privacy_gateway is not None:
        kwargs["privacy_gateway"] = privacy_gateway
    if config is not None:
        kwargs["config"] = config
    return await implementation(client, query, **kwargs)


def _run_async(coro_factory, timeout: int = 45):
    """Run an async Yahoo call from both sync tools and running event loops."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        # Preserve request-local privacy/turn ContextVars when a synchronous
        # caller invokes the bridge from an already-running event loop.
        context = contextvars.copy_context()
        future = executor.submit(
            lambda: context.run(asyncio.run, coro_factory())
        )
        return future.result(timeout=timeout)


def _value(item: Any, name: str, default: Any = "") -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def yahoo_posts(result: Any) -> list[Any]:
    """Extract canonical Yahoo posts without depending on a concrete class."""

    if result is None:
        return []
    posts = _value(result, "posts", None)
    if posts is None and isinstance(result, (list, tuple)):
        posts = result
    if not isinstance(posts, (list, tuple)):
        return []
    return [post for post in posts if post is not None]


def yahoo_result_has_results(result: Any) -> bool:
    """Use the service's explicit success flag, falling back to post count."""

    posts = yahoo_posts(result)
    flag = _value(result, "has_results", None)
    # A positive flag is meaningful only when actual posts accompany it;
    # callers must be able to fall back after a malformed/empty envelope.
    return bool(posts) and (flag is None or bool(flag))


def format_yahoo_x_results(
    query: str,
    result: Any,
    *,
    heading: str = "Yahoo!リアルタイム検索",
    max_results: int | None = None,
) -> str:
    """Format canonical Yahoo posts with URL, body, author, and timestamp."""

    posts = yahoo_posts(result)
    if max_results is not None:
        try:
            posts = posts[: max(1, min(int(max_results), 25))]
        except (TypeError, ValueError):
            pass
    if not posts:
        return "Yahoo!リアルタイム検索では該当するX投稿を取得できませんでした。"

    lines = [f"{heading}: {str(query or '').strip()}", ""]
    for index, post in enumerate(posts, start=1):
        text = str(_value(post, "text", "") or "").strip()
        url = str(_value(post, "url", "") or "").strip()
        author_name = str(_value(post, "author_name", "") or "").strip()
        author_handle = str(_value(post, "author_handle", "") or "").strip()
        published_at = str(_value(post, "published_at", "") or "").strip()

        # Keep each post readable even if Yahoo omits one optional field.
        lines.append(f"[{index}] 本文: {text or '(本文なし)'}")
        if author_handle and not author_handle.startswith("@"):
            author_handle = f"@{author_handle}"
        author = " ".join(value for value in (author_name, author_handle) if value)
        if author:
            lines.append(f"投稿者: {author}")
        if published_at:
            lines.append(f"日時: {published_at}")
        if url:
            lines.append(f"URL: {url}")
        lines.append("")
    return "\n".join(lines).strip()


def search_yahoo_realtime_sync(
    query: str,
    *,
    max_results: int = 8,
    timeout_seconds: int = 45,
    client: Any = None,
    privacy_gateway: Any = None,
    config: Any = None,
):
    """Synchronous bridge used by normal-chat tools."""

    limit = max(1, min(int(max_results), 25))
    return _run_async(
        lambda: search_yahoo_realtime(
            client,
            str(query or "").strip(),
            limit=limit,
            privacy_gateway=privacy_gateway,
            config=config,
        ),
        timeout=max(1, int(timeout_seconds)),
    )


def x_search_impl(
    query: str,
    *,
    max_results: int = 8,
    timeout_seconds: int = 45,
    config: Any = None,
) -> str:
    """Run Yahoo realtime X search and render citation-ready posts."""

    if not str(query or "").strip():
        return "検索クエリを指定してください。"
    try:
        result = search_yahoo_realtime_sync(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 - tool boundary must remain usable
        return f"X検索（Yahooリアルタイム）に失敗しました: {exc}"
    status = str(_value(result, "status", "") or "").strip().lower()
    if status in {"blocked", "privacy_blocked"}:
        return "X検索（Yahooリアルタイム）はプライバシーポリシーにより停止しました。"
    return format_yahoo_x_results(query, result, max_results=max_results)


@tool
def x_search(
    query: str,
    max_results: int = 8,
    timeout_seconds: int = 45,
) -> str:
    """Yahooリアルタイム検索でXの投稿を調査します。

    Args:
        query: X上で検索するキーワードまたは投稿URL
        max_results: 最大取得件数（1〜25）
        timeout_seconds: 検索のタイムアウト秒数
    """

    return x_search_impl(
        query,
        max_results=max_results,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "x_search",
    "x_search_impl",
    "search_yahoo_realtime",
    "search_yahoo_realtime_sync",
    "format_yahoo_x_results",
    "yahoo_posts",
    "yahoo_result_has_results",
    "is_x_url",
    "looks_like_x_search_request",
]
