"""Structured telemetry for Docs hybrid search paths."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class DocsIndexSearchTelemetry:
    """Semantic lane telemetry from ``DocsIndexService.search``."""

    dense_used: bool
    sparse_used: bool
    fusion: str
    candidate_count: int
    latency_ms: float
    fallback_reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DocsSearchTelemetry:
    """End-to-end telemetry for ``docs_search`` merge."""

    retrieval_mode: str
    lexical_attempted: bool
    semantic_attempted: bool
    lexical_count: int
    semantic_count: int
    merged_count: int
    latency_ms: float
    index: Optional[DocsIndexSearchTelemetry] = None
    fallback_reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.index is not None:
            payload["index"] = self.index.as_dict()
        return payload


def build_docs_search_telemetry(
    *,
    query: str,
    lexical_count: int,
    semantic_count: int,
    merged_count: int,
    latency_ms: float,
    index: Optional[DocsIndexSearchTelemetry],
    docs_rag_enabled: bool,
) -> DocsSearchTelemetry:
    lexical_attempted = True
    semantic_attempted = bool(str(query or "").strip()) and docs_rag_enabled
    if lexical_attempted and semantic_attempted:
        retrieval_mode = "hybrid_execution"
    elif semantic_attempted:
        retrieval_mode = "semantic_execution"
    else:
        retrieval_mode = "lexical_execution"
    fallback_reason = None
    if semantic_attempted and semantic_count == 0 and index is not None:
        fallback_reason = index.fallback_reason
    return DocsSearchTelemetry(
        retrieval_mode=retrieval_mode,
        lexical_attempted=lexical_attempted,
        semantic_attempted=semantic_attempted,
        lexical_count=lexical_count,
        semantic_count=semantic_count,
        merged_count=merged_count,
        latency_ms=latency_ms,
        index=index,
        fallback_reason=fallback_reason,
    )
