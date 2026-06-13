"""Lightweight local search service used by the normal web_search tool."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Iterable

from .deep_research_service import DeepResearchSearchClient, DeepResearchSource


SEARCH_PROVIDER_OPENAI = "openai"
SEARCH_PROVIDER_LOCAL = "local"
SEARCH_PROVIDER_VALUES = {SEARCH_PROVIDER_OPENAI, SEARCH_PROVIDER_LOCAL}
DEFAULT_SEARCH_PROVIDER = SEARCH_PROVIDER_OPENAI
DEFAULT_LOCAL_SEARCH_ENGINES = ["searxng", "wikipedia"]
DEFAULT_LOCAL_SEARCH_MAX_RESULTS = 5

_JA_LEADING_SEARCH_COMMAND_RE = re.compile(
    r"^(?:\u5ff5\u306e\u305f\u3081|\u4e00\u5fdc|\u3067\u304d\u308c\u3070)?\s*"
    r"(?:\u691c\u7d22(?:\u3057\u3066)?|\u8abf\u3079\u3066|\u78ba\u8a8d(?:\u3057\u3066)?)"
    r"[\u3001,]\s*"
)
_JA_TRAILING_SEARCH_COMMAND_RE = re.compile(
    r"(?:\u3092)?"
    r"(?:\u8abf\u3079\u3066|\u691c\u7d22\u3057\u3066|\u78ba\u8a8d\u3057\u3066|"
    r"\u6559\u3048\u3066|\u7b54\u3048\u3066)"
    r"(?:\u304f\u3060\u3055\u3044|\u4e0b\u3055\u3044|\u304f\u308c)?"
    r"[\u3002.!！?？\s]*$"
)
_EN_LEADING_SEARCH_COMMAND_RE = re.compile(
    r"^(?:please\s+)?(?:search|look up|check|verify|find)\s+(?:for\s+)?",
    re.IGNORECASE,
)


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    if hasattr(config, "get"):
        try:
            return config.get(key, default)
        except Exception:
            return default
    return default


def normalize_search_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    return provider if provider in SEARCH_PROVIDER_VALUES else DEFAULT_SEARCH_PROVIDER


def get_search_provider(config: Any = None) -> str:
    return normalize_search_provider(
        _config_get(config, "search.provider", DEFAULT_SEARCH_PROVIDER)
    )


def get_local_search_engines(config: Any = None) -> list[str]:
    raw = _config_get(config, "search.local_engines", DEFAULT_LOCAL_SEARCH_ENGINES)
    if isinstance(raw, str):
        engines = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, Iterable):
        engines = [str(item).strip() for item in raw]
    else:
        engines = list(DEFAULT_LOCAL_SEARCH_ENGINES)
    return [engine for engine in engines if engine] or list(DEFAULT_LOCAL_SEARCH_ENGINES)


def get_local_search_max_results(config: Any = None) -> int:
    raw = _config_get(config, "search.local_max_results", DEFAULT_LOCAL_SEARCH_MAX_RESULTS)
    try:
        value = int(raw)
    except Exception:
        value = DEFAULT_LOCAL_SEARCH_MAX_RESULTS
    return max(1, min(value, 10))


def _truncate(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "..."


def normalize_local_search_query(query: str) -> str:
    """Convert delegated search requests into compact search keywords."""
    original = re.sub(r"\s+", " ", str(query or "")).strip()
    if not original:
        return ""

    normalized = original.strip(" \t\r\n\"'`")
    normalized = _JA_LEADING_SEARCH_COMMAND_RE.sub("", normalized)
    normalized = _EN_LEADING_SEARCH_COMMAND_RE.sub("", normalized)
    normalized = _JA_TRAILING_SEARCH_COMMAND_RE.sub("", normalized)
    normalized = normalized.strip(" \t\r\n\"'`。、,.")

    return normalized or original


def format_local_search_results(query: str, sources: list[DeepResearchSource]) -> str:
    if not sources:
        return (
            "汎用Web検索結果は見つかりませんでした。"
            "SearXNG設定、ネットワーク、検索語を確認してください。"
        )

    lines = [f"汎用Web検索結果: {query}", ""]
    for source in sources:
        lines.append(f"[{source.id}] {source.title}")
        if source.url:
            lines.append(f"URL: {source.url}")
        if source.published_at:
            lines.append(f"公開日: {source.published_at}")
        if source.snippet:
            lines.append(f"概要: {_truncate(source.snippet, 420)}")
        lines.append(f"検索元: {source.engine}")
        lines.append("")
    return "\n".join(lines).strip()


async def local_web_search_async(query: str, config: Any = None) -> str:
    max_results = get_local_search_max_results(config)
    client = DeepResearchSearchClient(config=config, timeout_seconds=10.0)
    normalized_query = normalize_local_search_query(query)
    candidates = [
        candidate
        for candidate in dict.fromkeys([normalized_query, str(query or "").strip()])
        if candidate
    ]

    last_query = normalized_query
    last_sources: list[DeepResearchSource] = []
    for candidate in candidates:
        sources = await client.search(
            candidate,
            engines=get_local_search_engines(config),
            max_results_per_engine=max_results,
            include_local_knowledge=bool(
                _config_get(config, "search.include_local_knowledge", False)
            ),
            project_id=_config_get(config, "search.project_id", None),
        )
        last_query = candidate
        last_sources = sources[:max_results]
        if last_sources:
            break

    return format_local_search_results(last_query, last_sources)


def _run_async(coro_factory):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(coro_factory()))
        return future.result(timeout=45)


def local_web_search(query: str, config: Any = None) -> str:
    return _run_async(lambda: local_web_search_async(query, config))
