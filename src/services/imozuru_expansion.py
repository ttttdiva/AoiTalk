"""Bounded public-evidence graph expansion for deep research.

Imozuru (芋づる式) follows citations, incoming citations, author paths, and
links from already public evidence.  It is intentionally a small, sequential
orchestrator: every HTML/JSON request goes through ``UrlIngestService`` and
every OpenAlex request goes through ``openalex_policy``.  No local knowledge,
private source, credential provider, or direct HTTP client is consulted here.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import ipaddress
import re
import time
from collections import deque
from dataclasses import dataclass, field, fields
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from .deep_research_service import DeepResearchSource
from .openalex_policy import (
    OPENALEX_ORIGIN,
    OpenAlexPolicy,
    OpenAlexPolicyError,
    OpenAlexRateLimiter,
    OpenAlexTransportResult,
    get_openalex_limiter,
)
from .outbound_privacy_service import (
    ExternalProviderBlocked,
    OutboundPrivacyGateway,
    PrivacyError,
)
from .url_ingest_service import UrlFetchResult, UrlIngestService


DEFAULT_MAX_HOPS = 2
DEFAULT_MAX_PAGES = 12
DEFAULT_MAX_SECONDS = 45.0
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_OPENALEX_REQUESTS = 8
DEFAULT_MAX_SOURCES = 64
MAX_EXCERPT_CHARS = 12_000
MAX_SNIPPET_CHARS = 2_000
MAX_LINKS_PER_PAGE = 32
MAX_WORKS_PER_API_RESULT = 8
_SAFE_ACQUISITION_STATUSES = frozenset(
    {
        "retrieved",
        "snippet_only",
        "privacy_blocked",
        "fetch_failed",
        "inaccessible",
        "empty_body",
        "unsupported_content",
        "byte_budget_exhausted",
        "parse_failed",
        "auth_required",
        "access_denied",
        "deleted",
        "rate_limited",
        "network_error",
        "ssrf_blocked",
        "time_limit",
        "openalex_unavailable",
        "openalex_rate_limited",
    }
)

_LOCAL_ENGINES = frozenset(
    {
        "local_knowledge",
        "local",
        "internal",
        "private",
        "memory",
        "knowledge",
    }
)
_OPENALEX_HOSTS = frozenset({"api.openalex.org", "openalex.org", "www.openalex.org"})
_DOI_HOSTS = frozenset({"doi.org", "dx.doi.org", "www.doi.org"})
_OPENALEX_ID_RE = re.compile(r"(?:https?://(?:www\.)?openalex\.org/)?([WwAaSsPpIiCc]\d+)$")
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.I)


@dataclass
class ExpansionResult:
    """Expansion output with bounded, safe diagnostics."""

    sources: list[DeepResearchSource] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": [
                source.to_dict() if hasattr(source, "to_dict") else _source_dict(source)
                for source in self.sources
            ],
            "metadata": copy.deepcopy(self.metadata),
        }


class _ExpansionStop(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "stopped")
        super().__init__(self.reason)


@dataclass
class _Candidate:
    url: str
    parent_id: int | None
    relation: str
    hop: int
    title: str = ""
    snippet: str = ""
    engine: str = "imozuru"
    query: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    kind: str = "page"


@dataclass
class _Fetched:
    requested_url: str
    final_url: str
    success: bool
    status: str
    title: str = ""
    body: str = ""
    snippet: str = ""
    author: str = ""
    published_at: str = ""
    external_links: list[str] = field(default_factory=list)
    json_data: Any = None
    status_code: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    error: str = ""


def _cfg(config: Any, key: str, default: Any) -> Any:
    if isinstance(config, Mapping):
        nested = config.get("imozuru_expansion")
        if isinstance(nested, Mapping) and key in nested:
            return nested.get(key)
        if key in config:
            return config.get(key)
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            nested = getter("imozuru_expansion", None)
            if isinstance(nested, Mapping) and key in nested:
                return nested.get(key)
            value = getter(key, default)
            if value is not None:
                return value
        except Exception:
            pass
    return default


def _clip(value: Any, limit: int = MAX_EXCERPT_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _source_dict(source: Any) -> dict[str, Any]:
    if isinstance(source, Mapping):
        return dict(source)
    result: dict[str, Any] = {}
    try:
        names = [item.name for item in fields(source)]
    except (TypeError, ValueError):
        names = ["id", "title", "url", "snippet", "engine", "query", "published_at", "raw"]
    for name in names:
        result[name] = getattr(source, name, None)
    for name in ("parent_id", "relation", "hop", "acquisition_status"):
        if hasattr(source, name):
            result[name] = getattr(source, name)
    return result


def _canonical_url(value: Any) -> str:
    """Strip fragments and normalize hostname only; preserve path/query case."""

    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    host = parts.hostname.casefold().rstrip(".")
    netloc = host
    try:
        if parts.port is not None:
            netloc = f"{host}:{parts.port}"
    except ValueError:
        return ""
    return urlunsplit(
        (
            parts.scheme.casefold(),
            netloc,
            parts.path or "/",
            parts.query,
            "",
        )
    )


def _identifier_url(value: Any, *, kind: str = "") -> str:
    """Convert a DOI/OpenAlex identifier into a public URL without guessing."""

    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("doi:"):
        text = text[4:].strip()
    if text.casefold().startswith("https://doi.org/") or text.casefold().startswith("http://doi.org/"):
        return _canonical_url(text)
    if _DOI_RE.match(text):
        return _canonical_url("https://doi.org/" + text)
    match = _OPENALEX_ID_RE.match(text)
    if match:
        return _canonical_url(f"https://openalex.org/{match.group(1).upper()}")
    if kind.casefold() in {"doi", "openalex", "openalex_id", "openalex_url"}:
        return _canonical_url(text)
    return ""


def _is_local_source(source: Any) -> bool:
    engine = str(getattr(source, "engine", "") or "").strip().casefold()
    if engine in _LOCAL_ENGINES or engine.startswith("local_"):
        return True
    raw = getattr(source, "raw", {})
    if not isinstance(raw, Mapping):
        return False
    marker = str(raw.get("source_kind") or raw.get("visibility") or "").strip().casefold()
    if marker in _LOCAL_ENGINES or marker in {"internal", "private", "local_only"}:
        return True
    if raw.get("privacy_scope") not in (None, "public", "web"):
        return True
    if any(raw.get(key) for key in ("private", "local", "document", "project_id")):
        return True
    provenance = raw.get("provenance")
    if isinstance(provenance, list):
        for item in provenance:
            if isinstance(item, Mapping) and str(item.get("engine") or "").casefold() in _LOCAL_ENGINES:
                return True
    return False


def _private_host(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return not ip.is_global
    except ValueError:
        return True


def _public_identifier_candidates(source: Any) -> list[str]:
    if _is_local_source(source):
        return []
    values: list[tuple[Any, str]] = [(getattr(source, "url", ""), "url")]
    raw = getattr(source, "raw", {})
    if isinstance(raw, Mapping):
        for key in (
            "doi",
            "DOI",
            "openalex_id",
            "openalex",
            "openalex_url",
            "work_id",
        ):
            values.append((raw.get(key), key))
    found: list[str] = []
    for value, kind in values:
        if kind in {"openalex_id", "openalex", "openalex_url", "work_id"}:
            ident = _openalex_id(value)
            candidate = (
                _canonical_url(f"{OPENALEX_ORIGIN}/works/{ident}") if ident else ""
            )
        else:
            candidate = _canonical_url(value)
        if not candidate:
            candidate = _identifier_url(value, kind=kind)
        if candidate and not _private_host(candidate) and candidate not in found:
            found.append(candidate)
    return found


def _openalex_id(value: Any) -> str:
    text = str(value or "").strip()
    match = _OPENALEX_ID_RE.search(text)
    if match:
        return match.group(1).upper()
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if (parts.hostname or "").casefold() in _OPENALEX_HOSTS:
        tail = parts.path.rstrip("/").split("/")[-1]
        return tail.upper() if re.fullmatch(r"[WASPiC]\d+", tail, re.I) else ""
    return ""


def _is_openalex_api_url(value: Any) -> bool:
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme.casefold() != "https" or (parts.hostname or "").casefold() != "api.openalex.org":
            return False
        if parts.username is not None or parts.password is not None:
            return False
        return parts.port in (None, 443)
    except ValueError:
        return False


def _doi(value: Any) -> str:
    text = str(value or "").strip()
    if text.casefold().startswith(("doi:", "https://doi.org/", "http://doi.org/")):
        text = text.split("/", 3)[-1] if "://" in text else text[4:]
    return text if _DOI_RE.match(text) else ""


def _result_status(value: Any) -> str:
    status = str(getattr(value, "acquisition_status", "") or "").strip().casefold()
    if status:
        return status if status in _SAFE_ACQUISITION_STATUSES else "fetch_failed"
    if bool(getattr(value, "success", False)):
        return "retrieved"
    return "fetch_failed"


def _normalize_fetch(value: Any, requested_url: str) -> _Fetched:
    if isinstance(value, _Fetched):
        return value
    if isinstance(value, Mapping):
        get = value.get
        success = bool(get("success", get("ok", False)))
        status = str(get("acquisition_status", get("status", "")) or "").casefold()
        payload = get("json_data", get("payload"))
        headers = get("response_headers", get("headers", {}))
        return _Fetched(
            requested_url=requested_url,
            final_url=str(get("final_url") or get("url") or requested_url),
            success=success,
            status=(status if status in _SAFE_ACQUISITION_STATUSES else ("retrieved" if success else "fetch_failed")),
            title=str(get("title") or get("og_title") or ""),
            body=str(get("body") or get("text") or ""),
            snippet=str(get("snippet") or ""),
            author=str(get("author") or ""),
            published_at=str(get("published_at") or ""),
            external_links=[str(item) for item in (get("external_links") or []) if item],
            json_data=payload,
            status_code=int(get("status_code") or 0),
            headers={str(k).lower(): str(v) for k, v in dict(headers or {}).items()},
            error=str(get("error") or ""),
        )
    return _Fetched(
        requested_url=requested_url,
        final_url=str(getattr(value, "final_url", "") or requested_url),
        success=bool(getattr(value, "success", False)),
        status=_result_status(value),
        title=str(getattr(value, "title", "") or getattr(value, "og_title", "") or ""),
        body=str(getattr(value, "body", "") or ""),
        snippet=str(getattr(value, "snippet", "") or ""),
        author=str(getattr(value, "author", "") or ""),
        published_at=str(getattr(value, "published_at", "") or ""),
        external_links=[str(item) for item in (getattr(value, "external_links", []) or []) if item],
        json_data=getattr(value, "json_data", None),
        status_code=int(getattr(value, "status_code", 0) or 0),
        headers={
            str(k).lower(): str(v)
            for k, v in dict(getattr(value, "response_headers", {}) or {}).items()
        },
        error=str(getattr(value, "error", "") or ""),
    )


def _safe_work_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    location = payload.get("primary_location")
    if not isinstance(location, Mapping):
        location = {}
    source = location.get("source") if isinstance(location.get("source"), Mapping) else {}
    authors: list[dict[str, str]] = []
    for item in payload.get("authorships") or []:
        if not isinstance(item, Mapping):
            continue
        author = item.get("author") if isinstance(item.get("author"), Mapping) else {}
        author_id = _openalex_id(author.get("id"))
        name = _clip(author.get("display_name"), 200)
        if author_id or name:
            authors.append({"id": author_id, "name": name})
        if len(authors) >= 16:
            break
    references: list[str] = []
    for value in payload.get("referenced_works") or []:
        ident = _openalex_id(value)
        if ident and ident not in references:
            references.append(ident)
        if len(references) >= 100:
            break
    abstract = _abstract_from_inverted_index(payload.get("abstract_inverted_index"))
    doi = _doi(payload.get("doi"))
    work_id = _openalex_id(payload.get("id"))
    cited_by_api_url = str(payload.get("cited_by_api_url") or "")
    if cited_by_api_url and not _is_openalex_api_url(cited_by_api_url):
        cited_by_api_url = ""
    landing = str(
        location.get("landing_page_url")
        or location.get("pdf_url")
        or (f"https://doi.org/{doi}" if doi else "")
        or payload.get("id")
        or ""
    )
    return {
        key: value
        for key, value in {
            "openalex_id": work_id,
            "doi": doi,
            "landing_url": _canonical_url(landing),
            "title": _clip(payload.get("title"), 500),
            "abstract": abstract,
            "publication_date": _clip(payload.get("publication_date"), 64),
            "cited_by_count": payload.get("cited_by_count"),
            "referenced_works": references,
            "authorships": authors,
            "cited_by_api_url": cited_by_api_url,
            "source_display_name": _clip(source.get("display_name"), 200),
        }.items()
        if value not in (None, "", [], {})
    }


def _abstract_from_inverted_index(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    words: list[tuple[int, str]] = []
    for token, positions in value.items():
        if not isinstance(positions, (list, tuple)):
            continue
        for position in positions[:32]:
            try:
                words.append((int(position), str(token)))
            except (TypeError, ValueError):
                continue
    words.sort(key=lambda item: item[0])
    return _clip(" ".join(token for _, token in words), MAX_SNIPPET_CHARS)


def _source_copy(source: Any, source_id: int) -> DeepResearchSource:
    values = _source_dict(source)
    values["id"] = source_id
    values.setdefault("published_at", None)
    values["raw"] = dict(values.get("raw") or {}) if isinstance(values.get("raw"), Mapping) else {}
    allowed = {item.name for item in fields(DeepResearchSource)}
    return DeepResearchSource(**{key: value for key, value in values.items() if key in allowed})


class ImozuruExpansion:
    """Expand public source evidence under finite page/time/byte budgets."""

    def __init__(
        self,
        config: Any = None,
        *,
        privacy_gateway: OutboundPrivacyGateway | Any,
        checkpoint: Callable[..., Any] | None = None,
        max_hops: int | None = None,
        max_pages: int | None = None,
        max_seconds: float | None = None,
        max_bytes: int | None = None,
        max_total_bytes: int | None = None,
        max_openalex_requests: int | None = None,
        max_sources: int | None = None,
        url_ingest_service: Any | None = None,
        openalex_limiter: OpenAlexRateLimiter | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config
        self.privacy_gateway = privacy_gateway or OutboundPrivacyGateway(config)
        self.checkpoint = checkpoint
        self.max_hops = max(0, min(int(max_hops if max_hops is not None else _cfg(config, "max_hops", DEFAULT_MAX_HOPS)), 2))
        self.max_pages = max(0, min(int(max_pages if max_pages is not None else _cfg(config, "max_pages", DEFAULT_MAX_PAGES)), 12))
        self.max_seconds = max(0.01, min(float(max_seconds if max_seconds is not None else _cfg(config, "max_seconds", DEFAULT_MAX_SECONDS)), 45.0))
        total_bytes = max_bytes if max_bytes is not None else max_total_bytes
        if total_bytes is None:
            total_bytes = _cfg(config, "max_bytes", DEFAULT_MAX_BYTES)
        self.max_bytes = max(0, min(int(total_bytes), DEFAULT_MAX_BYTES))
        self.max_openalex_requests = max(0, min(int(max_openalex_requests if max_openalex_requests is not None else _cfg(config, "max_openalex_requests", DEFAULT_MAX_OPENALEX_REQUESTS)), 8))
        self.max_sources = max(1, min(int(max_sources if max_sources is not None else _cfg(config, "max_sources", DEFAULT_MAX_SOURCES)), 128))
        self._clock = clock or time.monotonic
        self._provided_url_service = url_ingest_service
        self._openalex_limiter = openalex_limiter or get_openalex_limiter()

    async def expand(
        self,
        sources: list[DeepResearchSource],
        query: str,
    ) -> ExpansionResult:
        started = self._clock()
        deadline = started + self.max_seconds
        source_list: list[DeepResearchSource] = []
        source_by_url: dict[str, DeepResearchSource] = {}
        queue: deque[_Candidate] = deque()
        seed_candidates: list[_Candidate] = []
        seen_urls: set[str] = set()
        seen_api_urls: set[str] = set()
        failures: list[dict[str, Any]] = []
        stop_reasons: list[str] = []
        counters = {
            "pages_attempted": 0,
            "pages_retrieved": 0,
            "openalex_requests": 0,
            "new_sources": 0,
            "links_seen": 0,
            "references_seen": 0,
            "incoming_seen": 0,
            "authors_seen": 0,
        }
        cancelled = False
        service = self._build_url_service()
        initial_source_count = len(sources or [])
        max_total_source_count = initial_source_count + self.max_sources
        policy = OpenAlexPolicy(
            limiter=self._openalex_limiter,
            max_requests=self.max_openalex_requests,
        )

        next_id = 1
        for item in list(sources or []):
            try:
                sid = int(getattr(item, "id", 0) or 0)
            except (TypeError, ValueError):
                sid = 0
            if sid <= 0 or any(int(getattr(existing, "id", 0) or 0) == sid for existing in source_list):
                sid = max(next_id, max((int(getattr(existing, "id", 0) or 0) for existing in source_list), default=0) + 1)
            next_id = max(next_id, sid + 1)
            copied = _source_copy(item, sid)
            source_list.append(copied)
            for candidate_url in _public_identifier_candidates(item):
                key = _canonical_url(candidate_url)
                if not key or key in source_by_url:
                    continue
                source_by_url[key] = copied
                seed_candidates.append(
                    _Candidate(
                        url=key,
                        parent_id=sid,
                        relation="seed",
                        hop=0,
                        title=str(getattr(copied, "title", "") or ""),
                        snippet=str(getattr(copied, "snippet", "") or ""),
                        engine=str(getattr(copied, "engine", "") or "") or "public",
                        query=str(getattr(copied, "query", "") or query or ""),
                        raw=dict(getattr(copied, "raw", {}) or {}),
                        kind="seed",
                    )
                )
                break

        # Preserve every input source in the result while reserving most of
        # the page budget for the graph discovered from a small deterministic
        # public frontier.  Enqueuing dozens of search rows first would spend
        # all twelve page reads before any citation/link edge is followed.
        frontier_limit = 0 if self.max_pages <= 0 else max(1, min(4, self.max_pages // 2 or 1))
        queue.extend(seed_candidates[:frontier_limit])
        selected_seed_ids = {
            candidate.parent_id for candidate in seed_candidates[:frontier_limit] if candidate.parent_id is not None
        }
        skipped_seed_count = 0
        for copied in source_list:
            source_id = int(getattr(copied, "id", 0) or 0)
            if source_id in selected_seed_ids or _is_local_source(copied):
                continue
            skipped_seed_count += 1
            raw = dict(getattr(copied, "raw", {}) or {})
            raw.setdefault("search_snippet", _clip(getattr(copied, "snippet", ""), MAX_SNIPPET_CHARS))
            raw.setdefault("acquisition_status", "snippet_only")
            copied.raw = raw

        try:
            while queue and len(source_list) < max_total_source_count:
                candidate = queue.popleft()
                await self._checkpoint("before_candidate")
                self._check_time(deadline)
                key = _canonical_url(candidate.url)
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                if candidate.hop > self.max_hops:
                    continue
                if candidate.kind == "seed":
                    existing = source_by_url.get(key)
                    if existing is not None:
                        await self._process_seed(
                            existing,
                            candidate,
                            service,
                            deadline=deadline,
                            counters=counters,
                            failures=failures,
                            queue=queue,
                            seen_urls=seen_urls,
                            source_by_url=source_by_url,
                            seen_api_urls=seen_api_urls,
                            policy=policy,
                            source_list=source_list,
                            source_limit=max_total_source_count,
                            next_id_ref=[next_id],
                            query=query,
                        )
                        next_id = max(next_id, max((int(getattr(item, "id", 0) or 0) for item in source_list), default=0) + 1)
                    continue
                if candidate.kind in {"openalex_work", "openalex_list"}:
                    await self._process_openalex_work(
                        candidate,
                        service,
                        policy,
                        deadline=deadline,
                        counters=counters,
                        failures=failures,
                        queue=queue,
                        seen_urls=seen_urls,
                        source_by_url=source_by_url,
                        seen_api_urls=seen_api_urls,
                        source_list=source_list,
                        source_limit=max_total_source_count,
                        query=query,
                    )
                    continue
                await self._process_page(
                    candidate,
                    service,
                    deadline=deadline,
                    counters=counters,
                    failures=failures,
                    queue=queue,
                    seen_urls=seen_urls,
                    source_by_url=source_by_url,
                    source_list=source_list,
                    source_limit=max_total_source_count,
                    query=query,
                )
        except _ExpansionStop as stop:
            stop_reasons.append(stop.reason)
            cancelled = stop.reason == "cancelled"
        except asyncio.CancelledError:
            # Task cancellation is an ownership signal from the runner and
            # must reach its caller; checkpoint-driven cancellation uses the
            # bounded ``_ExpansionStop`` path above.
            await self._cancel_service(service)
            raise
        if skipped_seed_count:
            stop_reasons.append("seed_frontier_limit")
        if queue and counters["new_sources"] >= self.max_sources:
            stop_reasons.append("source_limit")
        elapsed = max(0.0, self._clock() - started)
        metadata = {
            "strategy": "imozuru",
            "partial": bool(queue or stop_reasons or failures),
            "cancelled": cancelled,
            "limits": {
                "max_hops": self.max_hops,
                "max_pages": self.max_pages,
                "max_seconds": self.max_seconds,
                "max_bytes": self.max_bytes,
                "max_openalex_requests": self.max_openalex_requests,
                "max_sources": self.max_sources,
                "max_new_sources": self.max_sources,
            },
            "hops": self.max_hops,
            "pages_used": counters["pages_attempted"],
            "pages_retrieved": counters["pages_retrieved"],
            "bytes_used": int(getattr(service, "bytes_used", 0) or 0),
            "bytes_remaining": getattr(service, "bytes_remaining", None),
            "openalex_requests": counters["openalex_requests"],
            "openalex_retries": policy.retries,
            "sources_before": len(sources or []),
            "sources_after": len(source_list),
            "seed_candidates": len(seed_candidates),
            "seed_frontier": min(frontier_limit, len(seed_candidates)),
            "seed_frontier_skipped": skipped_seed_count,
            "new_sources": counters["new_sources"],
            "source_limit_scope": "new_sources_only",
            "links_seen": counters["links_seen"],
            "references_seen": counters["references_seen"],
            "incoming_seen": counters["incoming_seen"],
            "authors_seen": counters["authors_seen"],
            "elapsed_seconds": round(elapsed, 3),
            "stop_reasons": sorted(set(stop_reasons)),
            "failures": _aggregate_failures(failures),
            "inaccessible_sources_are_retained_as_metadata": True,
            "retention": "bounded excerpts only; search snippets remain separate",
        }
        return ExpansionResult(sources=source_list, metadata=metadata)

    def _build_url_service(self) -> Any:
        if self._provided_url_service is not None:
            return self._provided_url_service
        try:
            return UrlIngestService(
                config=self.config,
                privacy_gateway=self.privacy_gateway,
                max_total_bytes=self.max_bytes,
                timeout_seconds=min(25.0, self.max_seconds),
                checkpoint=self._checkpoint,
            )
        except TypeError:
            # Keep compatibility with a narrow embedding double while retaining
            # the privacy gateway on the real implementation.
            return UrlIngestService(config=self.config, privacy_gateway=self.privacy_gateway)

    async def _process_seed(
        self,
        source: DeepResearchSource,
        candidate: _Candidate,
        service: Any,
        *,
        deadline: float,
        counters: dict[str, int],
        failures: list[dict[str, Any]],
        queue: deque[_Candidate],
        seen_urls: set[str],
        source_by_url: dict[str, DeepResearchSource],
        seen_api_urls: set[str],
        policy: OpenAlexPolicy,
        source_list: list[DeepResearchSource],
        source_limit: int,
        next_id_ref: list[int],
        query: str,
    ) -> None:
        if _is_local_source(source):
            return
        parsed = urlsplit(candidate.url)
        if (parsed.hostname or "").casefold() == "api.openalex.org" and parsed.path.startswith("/works"):
            result, payload = await self._fetch_openalex(
                service,
                policy,
                candidate.url,
                deadline=deadline,
                counters=counters,
                failures=failures,
            )
            if result.ok and isinstance(payload, Mapping):
                metadata = _safe_work_metadata(payload)
                self._annotate_openalex(source, metadata)
                raw = dict(getattr(source, "raw", {}) or {})
                raw.setdefault("acquisition_status", "snippet_only")
                source.raw = raw
                self._enqueue_openalex_graph(
                    metadata,
                    parent_id=int(getattr(source, "id", 0) or 0),
                    hop=candidate.hop,
                    queue=queue,
                    seen_urls=seen_urls,
                    seen_api_urls=seen_api_urls,
                    query=query,
                    counters=counters,
                )
            else:
                raw = dict(getattr(source, "raw", {}) or {})
                raw["acquisition_status"] = getattr(result, "error", "") or "fetch_failed"
                source.raw = raw
            return
        fetched = await self._fetch_page(
            service,
            candidate.url,
            deadline=deadline,
            counters=counters,
            failures=failures,
        )
        self._annotate_existing(source, fetched)
        final_key = _canonical_url(fetched.final_url)
        if final_key:
            source_by_url.setdefault(final_key, source)
            seen_urls.add(final_key)
        self._enqueue_links(
            fetched,
            parent_id=int(getattr(source, "id", 0) or 0),
            hop=candidate.hop,
            query=query,
            queue=queue,
            seen_urls=seen_urls,
            counters=counters,
        )
        await self._expand_openalex_for_source(
            source,
            candidate,
            service,
            policy,
            deadline=deadline,
            counters=counters,
            failures=failures,
            queue=queue,
            seen_urls=seen_urls,
            seen_api_urls=seen_api_urls,
            query=query,
        )

    async def _process_page(
        self,
        candidate: _Candidate,
        service: Any,
        *,
        deadline: float,
        counters: dict[str, int],
        failures: list[dict[str, Any]],
        queue: deque[_Candidate],
        seen_urls: set[str],
        source_by_url: dict[str, DeepResearchSource],
        source_list: list[DeepResearchSource],
        source_limit: int,
        query: str,
    ) -> None:
        if len(source_list) >= source_limit:
            return
        fetched = await self._fetch_page(
            service,
            candidate.url,
            deadline=deadline,
            counters=counters,
            failures=failures,
        )
        key = _canonical_url(candidate.url)
        existing = source_by_url.get(key)
        if not fetched.success:
            if existing is not None:
                existing.raw = dict(getattr(existing, "raw", {}) or {})
                existing.raw.setdefault("article_fetch_status", fetched.status)
                return
            if candidate.snippet or candidate.title:
                self._add_source(
                    candidate,
                    source_list,
                    source_by_url,
                    source_limit=source_limit,
                    counters=counters,
                    acquisition_status="snippet_only" if candidate.snippet else fetched.status,
                    acquired_excerpt="",
                    metadata_excerpt="",
                )
            return
        if existing is not None:
            self._annotate_existing(
                existing,
                fetched,
                parent_id=candidate.parent_id,
                relation=candidate.relation,
                hop=candidate.hop,
            )
            final_key = _canonical_url(fetched.final_url)
            if final_key:
                source_by_url[final_key] = existing
                seen_urls.add(final_key)
            self._enqueue_links(
                fetched,
                parent_id=int(getattr(existing, "id", 0) or 0),
                hop=candidate.hop,
                query=query,
                queue=queue,
                seen_urls=seen_urls,
                counters=counters,
            )
            return
        added = self._add_source(
            candidate,
            source_list,
            source_by_url,
            source_limit=source_limit,
            counters=counters,
            acquisition_status="retrieved",
            acquired_excerpt=fetched.body,
            metadata_excerpt="",
            fetched=fetched,
        )
        parent_id = int(getattr(added, "id", 0) or 0) or candidate.parent_id
        self._enqueue_links(
            fetched,
            parent_id=parent_id,
            hop=candidate.hop,
            query=query,
            queue=queue,
            seen_urls=seen_urls,
            counters=counters,
        )

    async def _process_openalex_work(
        self,
        candidate: _Candidate,
        service: Any,
        policy: OpenAlexPolicy,
        *,
        deadline: float,
        counters: dict[str, int],
        failures: list[dict[str, Any]],
        queue: deque[_Candidate],
        seen_urls: set[str],
        seen_api_urls: set[str],
        source_by_url: dict[str, DeepResearchSource],
        source_list: list[DeepResearchSource],
        source_limit: int,
        query: str,
    ) -> None:
        result, payload = await self._fetch_openalex(
            service,
            policy,
            candidate.url,
            deadline=deadline,
            counters=counters,
            failures=failures,
        )
        if not result.ok or not isinstance(payload, Mapping):
            if candidate.snippet or candidate.title:
                self._add_source(
                    candidate,
                    source_list,
                    source_by_url,
                    source_limit=source_limit,
                    counters=counters,
                    acquisition_status="snippet_only" if candidate.snippet else result.error or "fetch_failed",
                    acquired_excerpt="",
                    metadata_excerpt="",
                )
            return
        if candidate.kind == "openalex_list":
            rows = payload.get("results")
            if not isinstance(rows, list):
                return
            for row in rows[:MAX_WORKS_PER_API_RESULT]:
                if not isinstance(row, Mapping):
                    continue
                metadata = _safe_work_metadata(row)
                work_url = str(metadata.get("landing_url") or row.get("id") or "")
                if not work_url:
                    continue
                work_candidate = _Candidate(
                    url=work_url,
                    parent_id=candidate.parent_id,
                    relation=candidate.relation,
                    hop=candidate.hop,
                    title=str(metadata.get("title") or work_url),
                    engine="openalex",
                    query=query,
                    raw={"openalex": metadata, "metadata_kind": "openalex"},
                    kind="page",
                )
                self._add_source(
                    work_candidate,
                    source_list,
                    source_by_url,
                    source_limit=source_limit,
                    counters=counters,
                    acquisition_status="snippet_only",
                    acquired_excerpt="",
                    metadata_excerpt=str(metadata.get("abstract") or ""),
                )
                if candidate.hop <= self.max_hops and _canonical_url(work_url) not in seen_urls:
                    queue.append(
                        _Candidate(
                            url=work_url,
                            parent_id=candidate.parent_id,
                            relation="article",
                            hop=candidate.hop,
                            title=work_candidate.title,
                            engine="openalex",
                            query=query,
                        )
                    )
                self._enqueue_openalex_graph(
                    metadata,
                    parent_id=candidate.parent_id,
                    hop=candidate.hop,
                    queue=queue,
                    seen_urls=seen_urls,
                    seen_api_urls=seen_api_urls,
                    query=query,
                    counters=counters,
                )
            return
        metadata = _safe_work_metadata(payload)
        work_url = str(metadata.get("landing_url") or candidate.url)
        work_candidate = _Candidate(
            url=work_url,
            parent_id=candidate.parent_id,
            relation=candidate.relation,
            hop=candidate.hop,
            title=str(metadata.get("title") or candidate.title or work_url),
            snippet="",
            engine="openalex",
            query=query,
            raw={"openalex": metadata, "metadata_kind": "openalex"},
            kind="page",
        )
        if len(source_list) < source_limit or _canonical_url(work_url) in source_by_url:
            self._add_source(
                work_candidate,
                source_list,
                source_by_url,
                source_limit=source_limit,
                counters=counters,
                acquisition_status="snippet_only",
                acquired_excerpt="",
                metadata_excerpt=str(metadata.get("abstract") or ""),
            )
        if candidate.hop <= self.max_hops and _canonical_url(work_url) not in seen_urls:
            queue.append(
                _Candidate(
                    url=work_url,
                    parent_id=candidate.parent_id,
                    relation="article",
                    hop=candidate.hop,
                    title=work_candidate.title,
                    engine="openalex",
                    query=query,
                )
            )
        self._enqueue_openalex_graph(
            metadata,
            parent_id=candidate.parent_id,
            hop=candidate.hop,
            queue=queue,
            seen_urls=seen_urls,
            seen_api_urls=seen_api_urls,
            query=query,
            counters=counters,
        )

    async def _expand_openalex_for_source(
        self,
        source: DeepResearchSource,
        candidate: _Candidate,
        service: Any,
        policy: OpenAlexPolicy,
        *,
        deadline: float,
        counters: dict[str, int],
        failures: list[dict[str, Any]],
        queue: deque[_Candidate],
        seen_urls: set[str],
        seen_api_urls: set[str],
        query: str,
    ) -> None:
        raw = getattr(source, "raw", {})
        if not isinstance(raw, Mapping):
            raw = {}
        nested = raw.get("openalex") if isinstance(raw.get("openalex"), Mapping) else {}
        work_id = _openalex_id(
            raw.get("openalex_id")
            or nested.get("openalex_id")
            or raw.get("openalex")
            or candidate.url
        )
        doi = _doi(raw.get("doi") or nested.get("doi") or candidate.url)
        endpoint = ""
        if work_id:
            endpoint = f"{OPENALEX_ORIGIN}/works/{quote(work_id, safe='') }"
        elif doi:
            endpoint = f"{OPENALEX_ORIGIN}/works/https://doi.org/{quote(doi, safe='') }"
        refs = raw.get("referenced_works") or nested.get("referenced_works")
        authors = raw.get("authorships") or nested.get("authorships")
        if endpoint and endpoint not in seen_api_urls:
            result, payload = await self._fetch_openalex(
                service,
                policy,
                endpoint,
                deadline=deadline,
                counters=counters,
                failures=failures,
            )
            seen_api_urls.add(endpoint)
            if result.ok and isinstance(payload, Mapping):
                metadata = _safe_work_metadata(payload)
                self._annotate_openalex(source, metadata)
                refs = metadata.get("referenced_works")
                authors = metadata.get("authorships")
                self._enqueue_openalex_graph(
                    metadata,
                    parent_id=int(getattr(source, "id", 0) or 0),
                    hop=candidate.hop,
                    queue=queue,
                    seen_urls=seen_urls,
                    seen_api_urls=seen_api_urls,
                    query=query,
                    counters=counters,
                )
        if isinstance(refs, (list, tuple)) or isinstance(authors, (list, tuple)):
            metadata = {
                "referenced_works": refs if isinstance(refs, (list, tuple)) else [],
                "authorships": authors if isinstance(authors, (list, tuple)) else [],
                "openalex_id": work_id,
                "cited_by_api_url": raw.get("cited_by_api_url") or nested.get("cited_by_api_url") or "",
            }
            self._enqueue_openalex_graph(
                metadata,
                parent_id=int(getattr(source, "id", 0) or 0),
                hop=candidate.hop,
                queue=queue,
                seen_urls=seen_urls,
                seen_api_urls=seen_api_urls,
                query=query,
                counters=counters,
            )

    async def _fetch_openalex(
        self,
        service: Any,
        policy: OpenAlexPolicy,
        endpoint: str,
        *,
        deadline: float,
        counters: dict[str, int],
        failures: list[dict[str, Any]],
    ) -> tuple[Any, Mapping[str, Any] | None]:
        self._check_time(deadline)
        try:
            result = await self._await_deadline(
                policy.request_json(
                    service,
                    endpoint,
                    checkpoint=lambda: self._checkpoint("openalex"),
                    deadline=deadline,
                ),
                deadline,
            )
            counters["openalex_requests"] = policy.requests
            if result.ok and isinstance(result.payload, Mapping):
                return result, result.payload
            failures.append({"status": "openalex_rate_limited" if result.status_code == 429 else (result.error or "openalex_unavailable"), "count": 1})
            return result, None
        except OpenAlexPolicyError as exc:
            counters["openalex_requests"] = policy.requests
            failures.append({"status": exc.code, "count": 1})
            return OpenAlexTransportResult(ok=False, error=exc.code), None
        except (ExternalProviderBlocked, PrivacyError):
            raise
        except asyncio.TimeoutError:
            await self._cancel_service(service)
            failures.append({"status": "time_limit", "count": 1})
            raise _ExpansionStop("time_limit")
        except _ExpansionStop:
            raise
        except asyncio.CancelledError:
            raise

    async def _fetch_page(
        self,
        service: Any,
        url: str,
        *,
        deadline: float,
        counters: dict[str, int],
        failures: list[dict[str, Any]],
    ) -> _Fetched:
        self._check_time(deadline)
        if self._page_budget_exhausted(counters):
            failures.append({"status": "page_limit", "count": 1})
            raise _ExpansionStop("page_limit")
        target = _canonical_url(url)
        if not target or _private_host(target):
            failures.append({"status": "ssrf_blocked", "count": 1})
            return _Fetched(url, target or url, False, "ssrf_blocked")
        counters["pages_attempted"] += 1
        try:
            method = getattr(service, "fetch_public", None)
            if not callable(method):
                raise RuntimeError("public_fetch_unavailable")
            value = method(target, kind="html")
            if inspect.isawaitable(value):
                value = await self._await_deadline(value, deadline)
            fetched = _normalize_fetch(value, target)
            if fetched.success:
                counters["pages_retrieved"] += 1
                return fetched
            failures.append({"status": fetched.status or "fetch_failed", "count": 1})
            if fetched.status == "privacy_blocked":
                return fetched
            return fetched
        except (ExternalProviderBlocked, PrivacyError):
            failures.append({"status": "privacy_blocked", "count": 1})
            return _Fetched(target, target, False, "privacy_blocked")
        except asyncio.TimeoutError:
            await self._cancel_service(service)
            failures.append({"status": "time_limit", "count": 1})
            raise _ExpansionStop("time_limit")
        except asyncio.CancelledError:
            raise
        except Exception:
            failures.append({"status": "fetch_failed", "count": 1})
            return _Fetched(target, target, False, "fetch_failed")

    def _page_budget_exhausted(self, counters: Mapping[str, int]) -> bool:
        return int(counters.get("pages_attempted", 0)) >= self.max_pages

    async def _await_deadline(self, awaitable: Awaitable[Any], deadline: float) -> Any:
        remaining = max(0.001, deadline - self._clock())
        task = asyncio.ensure_future(awaitable)
        done, pending = await asyncio.wait({task}, timeout=remaining)
        if pending:
            task.cancel()
            task.add_done_callback(_consume_task)
            raise asyncio.TimeoutError
        return task.result()

    async def _cancel_service(self, service: Any) -> None:
        """Ask an injected transport to stop an in-flight read promptly."""

        cancel = getattr(service, "cancel", None)
        if not callable(cancel):
            return
        try:
            value = cancel()
            if not inspect.isawaitable(value):
                return
            task = asyncio.ensure_future(value)
            done, pending = await asyncio.wait({task}, timeout=0.25)
            if pending:
                task.cancel()
                task.add_done_callback(_consume_task)
        except Exception:
            return

    async def _checkpoint(self, stage: str) -> None:
        if self.checkpoint is None:
            return
        try:
            signature = inspect.signature(self.checkpoint)
            accepts_positional = any(
                parameter.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
                for parameter in signature.parameters.values()
            )
        except (TypeError, ValueError):
            accepts_positional = True
        value = self.checkpoint(stage) if accepts_positional else self.checkpoint()
        if inspect.isawaitable(value):
            value = await value
        if value is False:
            raise _ExpansionStop("cancelled")

    def _check_time(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise _ExpansionStop("time_limit")

    def _annotate_existing(
        self,
        source: DeepResearchSource,
        fetched: _Fetched,
        *,
        parent_id: int | None = None,
        relation: str = "seed",
        hop: int = 0,
    ) -> None:
        raw = dict(getattr(source, "raw", {}) or {})
        raw.setdefault("search_snippet", _clip(getattr(source, "snippet", ""), MAX_SNIPPET_CHARS))
        raw["acquisition_status"] = "retrieved" if fetched.success else fetched.status
        if fetched.success and fetched.body:
            raw["acquired_excerpt"] = _clip(fetched.body)
        if fetched.author:
            raw["citation_author"] = _clip(fetched.author, 300)
        if fetched.published_at:
            raw["citation_published_at"] = _clip(fetched.published_at, 64)
        raw.setdefault(
            "expansion",
            {
                "parent_id": parent_id,
                "relation": relation,
                "hop": hop,
            },
        )
        if relation == "article" and fetched.success:
            raw["acquisition_kind"] = "article_html"
        source.raw = raw

    def _annotate_openalex(self, source: DeepResearchSource, metadata: Mapping[str, Any]) -> None:
        raw = dict(getattr(source, "raw", {}) or {})
        raw.setdefault("search_snippet", _clip(getattr(source, "snippet", ""), MAX_SNIPPET_CHARS))
        raw["metadata_kind"] = "openalex"
        for key in ("openalex_id", "doi", "landing_url", "abstract", "publication_date", "cited_by_count", "referenced_works", "authorships", "cited_by_api_url"):
            if metadata.get(key) not in (None, "", [], {}):
                raw[key] = copy.deepcopy(metadata[key])
        source.raw = raw

    def _add_source(
        self,
        candidate: _Candidate,
        source_list: list[DeepResearchSource],
        source_by_url: dict[str, DeepResearchSource],
        *,
        source_limit: int,
        counters: dict[str, int],
        acquisition_status: str,
        acquired_excerpt: str,
        metadata_excerpt: str,
        fetched: _Fetched | None = None,
    ) -> DeepResearchSource | None:
        key = _canonical_url(candidate.url)
        final_key = _canonical_url(fetched.final_url) if fetched else ""
        existing = source_by_url.get(key) or source_by_url.get(final_key)
        if existing is not None:
            raw_existing = dict(getattr(existing, "raw", {}) or {})
            expansion = raw_existing.get("expansion")
            if not isinstance(expansion, Mapping):
                expansion = {}
            expansion = dict(expansion)
            provenance = raw_existing.get("expansion_provenance")
            if not isinstance(provenance, list):
                provenance = []
            edge = {
                "parent_id": candidate.parent_id,
                "relation": candidate.relation,
                "hop": candidate.hop,
            }
            if edge not in provenance:
                provenance.append(edge)
            relations = list(expansion.get("relations") or [])
            first_relation = expansion.get("relation")
            if first_relation and first_relation not in relations:
                relations.append(first_relation)
            if candidate.relation not in relations:
                relations.append(candidate.relation)
            expansion["relations"] = relations[:8]
            raw_existing["expansion"] = expansion
            raw_existing["expansion_provenance"] = provenance[:8]
            existing.raw = raw_existing
            return existing
        if len(source_list) >= source_limit:
            return None
        next_id = max((int(getattr(item, "id", 0) or 0) for item in source_list), default=0) + 1
        raw = dict(candidate.raw or {})
        raw["search_snippet"] = _clip(candidate.snippet, MAX_SNIPPET_CHARS)
        raw["acquisition_status"] = str(acquisition_status or "fetch_failed")
        if acquired_excerpt:
            raw["acquired_excerpt"] = _clip(acquired_excerpt)
        if metadata_excerpt:
            raw["metadata_excerpt"] = _clip(metadata_excerpt, MAX_SNIPPET_CHARS)
        if fetched is not None:
            if fetched.author:
                raw["citation_author"] = _clip(fetched.author, 300)
            if fetched.published_at:
                raw["citation_published_at"] = _clip(fetched.published_at, 64)
        raw["expansion"] = {
            "parent_id": candidate.parent_id,
            "relation": candidate.relation,
            "hop": candidate.hop,
        }
        source = DeepResearchSource(
            id=next_id,
            title=_clip(candidate.title or (fetched.title if fetched else "") or candidate.url, 500),
            url=_canonical_url(fetched.final_url if fetched and fetched.final_url else candidate.url) or candidate.url,
            snippet=_clip(candidate.snippet, MAX_SNIPPET_CHARS),
            engine=candidate.engine or "imozuru",
            query=candidate.query,
            published_at=(fetched.published_at if fetched and fetched.published_at else None),
            raw=raw,
        )
        # Parent revisions may expose first-class graph fields.  Keep the
        # module compatible with the baseline dataclass by attaching only when
        # the object permits it; raw expansion metadata remains canonical.
        for name, value in (
            ("parent_id", candidate.parent_id),
            ("relation", candidate.relation),
            ("hop", candidate.hop),
            ("acquisition_status", acquisition_status),
        ):
            try:
                setattr(source, name, value)
            except Exception:
                pass
        source_list.append(source)
        counters["new_sources"] += 1
        if key:
            source_by_url[key] = source
        if final_key:
            source_by_url[final_key] = source
        return source

    def _enqueue_links(
        self,
        fetched: _Fetched,
        *,
        parent_id: int | None,
        hop: int,
        query: str,
        queue: deque[_Candidate],
        seen_urls: set[str],
        counters: dict[str, int],
    ) -> None:
        if hop >= self.max_hops or not fetched.success:
            return
        links: list[str] = []
        for value in fetched.external_links[:MAX_LINKS_PER_PAGE]:
            key = _canonical_url(value)
            if key and key not in seen_urls and key not in links and not _private_host(key):
                links.append(key)
        counters["links_seen"] += len(links)
        for value in links:
            relation = "link"
            candidate_url = value
            candidate_kind = "page"
            candidate_engine = "imozuru_link"
            openalex_ident = _openalex_id(value)
            doi_ident = _doi(value)
            try:
                linked_host = (urlsplit(value).hostname or "").casefold()
            except ValueError:
                linked_host = ""
            if openalex_ident and linked_host in {"openalex.org", "www.openalex.org"}:
                if openalex_ident.startswith("A"):
                    candidate_url = f"{OPENALEX_ORIGIN}/works?filter=author.id:{openalex_ident}&per-page=4"
                    candidate_kind = "openalex_list"
                    relation = "author"
                else:
                    candidate_url = f"{OPENALEX_ORIGIN}/works/{openalex_ident}"
                    candidate_kind = "openalex_work"
                    relation = "reference"
                candidate_engine = "openalex"
            elif doi_ident:
                candidate_url = f"{OPENALEX_ORIGIN}/works/https://doi.org/{quote(doi_ident, safe='')}"
                candidate_kind = "openalex_work"
                candidate_engine = "openalex"
                relation = "reference"
            queue.append(
                _Candidate(
                    url=candidate_url,
                    parent_id=parent_id,
                    relation=relation,
                    hop=hop + 1,
                    engine=candidate_engine,
                    query=query,
                    kind=candidate_kind,
                )
            )

    def _enqueue_openalex_graph(
        self,
        metadata: Mapping[str, Any],
        *,
        parent_id: int | None,
        hop: int,
        queue: deque[_Candidate],
        seen_urls: set[str],
        seen_api_urls: set[str],
        query: str,
        counters: dict[str, int],
    ) -> None:
        if hop >= self.max_hops:
            return
        child_hop = hop + 1
        work_ids = [str(item) for item in (metadata.get("referenced_works") or []) if _openalex_id(item)]
        for ident in work_ids[:MAX_WORKS_PER_API_RESULT]:
            counters["references_seen"] += 1
            api_url = f"{OPENALEX_ORIGIN}/works/{_openalex_id(ident)}"
            if api_url in seen_api_urls:
                continue
            seen_api_urls.add(api_url)
            queue.append(
                _Candidate(
                    url=api_url,
                    parent_id=parent_id,
                    relation="reference",
                    hop=child_hop,
                    engine="openalex",
                    query=query,
                    kind="openalex_work",
                )
            )
        cited_url = str(metadata.get("cited_by_api_url") or "")
        if not cited_url:
            work_id = _openalex_id(metadata.get("openalex_id"))
            if work_id:
                cited_url = f"{OPENALEX_ORIGIN}/works?filter=cites:{work_id}&per-page=4"
        if _is_openalex_api_url(cited_url) and cited_url not in seen_api_urls:
            seen_api_urls.add(cited_url)
            counters["incoming_seen"] += 1
            queue.append(
                _Candidate(
                    url=cited_url,
                    parent_id=parent_id,
                    relation="incoming_citation",
                    hop=child_hop,
                    engine="openalex",
                    query=query,
                    kind="openalex_list",
                )
            )
        authors = metadata.get("authorships") or []
        for item in authors[:4] if isinstance(authors, list) else []:
            author_id = _openalex_id(item.get("id") if isinstance(item, Mapping) else item)
            if not author_id:
                continue
            counters["authors_seen"] += 1
            endpoint = f"{OPENALEX_ORIGIN}/works?filter=author.id:{author_id}&per-page=4"
            if endpoint in seen_api_urls:
                continue
            seen_api_urls.add(endpoint)
            queue.append(
                _Candidate(
                    url=endpoint,
                    parent_id=parent_id,
                    relation="author",
                    hop=child_hop,
                    engine="openalex",
                    query=query,
                    kind="openalex_list",
                )
            )


def _aggregate_failures(failures: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in failures:
        candidate = str(item.get("status") or "fetch_failed")
        status = candidate if candidate in _SAFE_ACQUISITION_STATUSES else "fetch_failed"
        counts[status] = counts.get(status, 0) + max(1, int(item.get("count", 1) or 1))
    return [{"status": key, "count": counts[key]} for key in sorted(counts)]


def _consume_task(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:
        return


__all__ = ["ExpansionResult", "ImozuruExpansion"]
