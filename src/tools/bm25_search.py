"""Runtime-facing BM25 search tool.

The implementation lives in :mod:`src.services.bm25_scope`; this thin module
keeps the tool import path stable for the runtime registry and CLI adapters.
"""

from __future__ import annotations

from src.services.bm25_scope import (
    BM25ScopeService,
    Bm25ScopeService,
    build_bm25_search_tool_definition,
    build_bm25_search_tool_definitions,
)

__all__ = [
    "Bm25ScopeService",
    "BM25ScopeService",
    "build_bm25_search_tool_definition",
    "build_bm25_search_tool_definitions",
]
