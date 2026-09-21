"""Lightweight local search service used by the normal web_search tool."""

from __future__ import annotations

import asyncio
import contextvars
import re
from typing import Any, Iterable

from .deep_research_service import DeepResearchSearchClient, DeepResearchSource
from .search_egress_policy import (
    PUBLIC_SEARCH_ENGINES,
    SearchEgressPreconditionError,
    assert_public_search_egress_approved,
    approved_public_egress,
    hosted_egress_approved,
    is_enterprise_profile,
    resolve_searxng_url,
    search_egress_error_message,
    strict_bool,
)


SEARCH_PROVIDER_OPENAI = "openai"
SEARCH_PROVIDER_LOCAL = "local"
SEARCH_PROVIDER_VALUES = {SEARCH_PROVIDER_OPENAI, SEARCH_PROVIDER_LOCAL}
DEFAULT_SEARCH_PROVIDER = SEARCH_PROVIDER_OPENAI
# SearXNG is the only implicit local route.  Public engines may still be
# selected explicitly through ``search.local_engines``; they are never added
# by this service when an internal endpoint is absent.
DEFAULT_LOCAL_SEARCH_ENGINES = ["searxng"]
DEFAULT_LOCAL_SEARCH_MAX_RESULTS = 5
_SEARCH_ENGINE_ALIASES = {
    "ddg": "duckduckgo",
    "duck_duck_go": "duckduckgo",
    "yahoo": "yahoo_realtime",
    "yahoo_realtime_search": "yahoo_realtime",
}


class SearchProviderConfigError(ValueError):
    """Raised when an Enterprise provider value is invalid."""

    code = "provider_invalid"

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
                break
            value = value[part]
        else:
            return value
    if hasattr(config, "get"):
        try:
            return config.get(key, default)
        except Exception:
            return default
    return default


def normalize_search_provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    if provider in SEARCH_PROVIDER_VALUES:
        return provider
    # Personal callers retain the historical fallback for compatibility.  An
    # Enterprise deployment must never turn an unknown/typoed provider into a
    # hosted OpenAI route by accident.
    try:
        from ..features import Features

        if Features.is_enterprise():
            raise SearchProviderConfigError("provider_invalid")
    except ImportError:
        pass
    return DEFAULT_SEARCH_PROVIDER


def get_search_provider(config: Any = None) -> str:
    configured = _config_get(config, "search.provider", None)
    provider = normalize_search_provider(configured or DEFAULT_SEARCH_PROVIDER)
    # Hosted Search is not a useful implicit default for an Enterprise/local
    # deployment.  Keep explicit Personal/hosted selections intact, but when
    # the main runtime is known to be local and no approved hosted-egress flag
    # is present, route normal search through the configured local engines.
    if provider == SEARCH_PROVIDER_OPENAI:
        try:
            from ..features import Features

            enterprise = bool(Features.is_enterprise())
        except Exception:
            enterprise = False
        llm_provider = str(
            _config_get(config, "llm_provider", "")
            or _config_get(config, "llm.provider", "")
            or ""
        ).strip().lower()
        # ``bool("false")`` is true; use the shared strict parser so a stale
        # string-valued setting can never opt an Enterprise deployment into a
        # hosted route by accident.
        hosted_approved = hosted_egress_approved(config)
        if (
            (configured is None
             or (isinstance(configured, str) and not configured.strip()))
            and (
                enterprise
                or llm_provider in {"ollama", "sglang", "openai_compatible_local"}
            )
            and not hosted_approved
        ):
            return SEARCH_PROVIDER_LOCAL
    return provider


def get_local_search_engines(config: Any = None) -> list[str]:
    raw = _config_get(config, "search.local_engines", DEFAULT_LOCAL_SEARCH_ENGINES)
    if isinstance(raw, str):
        engines = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, Iterable):
        engines = [str(item).strip() for item in raw]
    else:
        engines = list(DEFAULT_LOCAL_SEARCH_ENGINES)
    normalized = [
        _SEARCH_ENGINE_ALIASES.get(
            engine.strip().lower().replace("-", "_"),
            engine.strip().lower().replace("-", "_"),
        )
        for engine in engines
        if str(engine).strip()
    ]
    return normalized or list(DEFAULT_LOCAL_SEARCH_ENGINES)


def _local_route_engines(config: Any = None) -> list[str]:
    """Resolve local search engines without adding public fallbacks.

    ``DeepResearchSearchClient`` historically inserted DuckDuckGo whenever a
    selected SearXNG endpoint was absent.  That is useful compatibility for
    Personal deployments, but it violates the Enterprise contract: an
    explicit local route must never turn into an unapproved public request.
    Enterprise therefore removes implicit public engines when SearXNG is
    configured and refuses a route with no internal engine.  Explicit public
    engines remain available only after the shared egress gate succeeds.
    """

    engines = get_local_search_engines(config)
    if not is_enterprise_profile():
        return engines

    searxng_url = resolve_searxng_url(config)
    public_engines = [
        engine for engine in engines if engine in PUBLIC_SEARCH_ENGINES
    ]
    if searxng_url:
        # A local SearXNG route is authoritative.  Without an explicit public
        # egress approval, discard default public entries (typically
        # Wikipedia) rather than letting the deep client start them.
        if public_engines and not approved_public_egress(config):
            engines = [engine for engine in engines if engine not in PUBLIC_SEARCH_ENGINES]
        else:
            for engine in public_engines:
                assert_public_search_egress_approved(config, engine=engine)
        return engines

    # No SearXNG endpoint: only an explicitly approved public route may run.
    # If no public engine was selected, the caller gets a fast local-route
    # precondition error instead of DeepResearchSearchClient auto-injecting
    # DuckDuckGo.
    if not public_engines:
        return []
    for engine in public_engines:
        assert_public_search_egress_approved(config, engine=engine)
    # Remove the missing SearXNG marker before handing the list to the deep
    # client.  Its legacy compatibility branch treats a selected-but-missing
    # SearXNG engine as permission to append DuckDuckGo; passing only the
    # explicitly selected public engines prevents that implicit injection.
    return [engine for engine in engines if engine != "searxng"]


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
    normalized_query = normalize_local_search_query(query)
    candidates = [
        candidate
        for candidate in dict.fromkeys([normalized_query, str(query or "").strip()])
        if candidate
    ]

    try:
        route_engines = _local_route_engines(config)
    except SearchEgressPreconditionError as exc:
        return search_egress_error_message(exc)
    include_local_knowledge = strict_bool(
        _config_get(config, "search.include_local_knowledge", False), False
    )
    # Enterprise with no configured SearXNG/public route may still use the
    # explicitly local knowledge index.  Avoid constructing the deep client
    # (and, importantly, avoid its HTTP context) when there is no route at all.
    if not route_engines and not include_local_knowledge:
        if is_enterprise_profile():
            return search_egress_error_message(
                SearchEgressPreconditionError("local_search_unavailable")
            )
        # Personal compatibility is retained for the historical client path;
        # its own service may choose a configured fallback engine.
        route_engines = get_local_search_engines(config)

    client = DeepResearchSearchClient(config=config, timeout_seconds=10.0)

    last_query = normalized_query
    last_sources: list[DeepResearchSource] = []
    for candidate in candidates:
        sources = await client.search(
            candidate,
            engines=route_engines,
            max_results_per_engine=max_results,
            include_local_knowledge=include_local_knowledge,
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

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    # The synchronous tool bridge may be called from an active event loop.
    # Carry the request-local privacy/permission ContextVars into the worker
    # thread instead of silently reverting to process defaults.
    context = contextvars.copy_context()
    future = executor.submit(context.run, lambda: asyncio.run(coro_factory()))
    try:
        return future.result(timeout=45)
    finally:
        if not future.done():
            future.cancel()
        # Do not wait for a provider coroutine that ignored cancellation; the
        # caller must receive a bounded, sanitized failure.  The underlying
        # request still has its own finite HTTP timeout and settles in the
        # background.
        executor.shutdown(wait=False, cancel_futures=True)


def local_web_search(query: str, config: Any = None) -> str:
    return _run_async(lambda: local_web_search_async(query, config))
