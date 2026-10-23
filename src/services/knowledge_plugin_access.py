"""Allowlisted Knowledge reads for external plugin adapters.

The existing Knowledge API keeps its historical DTOs.  This module adds a
separate projection that never serializes KnowledgeSource/KnowledgeDocument
models directly and always evaluates source ACLs with ``is_admin=False``.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from ..knowledge.service import KnowledgeSearchFilters, KnowledgeService


DEFAULT_READ_CHARS = 16_000
MAX_READ_CHARS = 32_000
MAX_SEARCH_RESULTS = 50
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


class KnowledgePluginNotFound(LookupError):
    """A source/document was missing or outside the caller's read scope."""


class KnowledgePluginContentChanged(ValueError):
    """A paged read fingerprint no longer matches current source content."""


def _safe_relative_path(value: Any) -> str | None:
    path = str(value or "").replace("\\", "/")
    if not path or path.startswith("/") or re.match(r"^[a-zA-Z]:", path) or "\x00" in path:
        return None
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _safe_source_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048:
        return None
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        # Accessing .port also rejects malformed port values.
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, "", ""))


def _source_dto(value: Any) -> dict[str, Any]:
    source_id = value.get("id") if isinstance(value, dict) else getattr(value, "id", None)
    name = value.get("name") if isinstance(value, dict) else getattr(value, "name", None)
    source_type = (
        value.get("source_type")
        if isinstance(value, dict)
        else getattr(value, "source_type", None)
    )
    return {
        "id": str(source_id),
        "name": str(name or ""),
        "type": str(source_type or ""),
    }


def _document_dto(value: Any) -> dict[str, Any]:
    document_id = value.get("id") if isinstance(value, dict) else getattr(value, "id", None)
    title = value.get("title") if isinstance(value, dict) else getattr(value, "title", None)
    path = value.get("path") if isinstance(value, dict) else getattr(value, "path", None)
    return {
        "id": str(document_id),
        "title": str(title or ""),
        "relative_path": _safe_relative_path(path),
    }


def _bounded_search_hit(hit: dict[str, Any]) -> dict[str, Any] | None:
    source = hit.get("source")
    document = hit.get("document")
    chunk = hit.get("chunk")
    if not isinstance(source, dict) or not isinstance(document, dict) or not isinstance(chunk, dict):
        return None
    try:
        score = float(hit.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    if not math.isfinite(score):
        score = 0.0
    heading_path = chunk.get("heading_path")
    if not isinstance(heading_path, list):
        heading_path = []
    try:
        chunk_index = int(chunk.get("chunk_index") or 0)
    except (TypeError, ValueError):
        chunk_index = 0
    result = {
        "score": score,
        "retrieval": str(hit.get("retrieval") or ""),
        "source": _source_dto(source),
        "document": _document_dto(document),
        "chunk": {
            "heading_path": [str(item)[:500] for item in heading_path[:16]],
            "chunk_index": chunk_index,
            "text": str(chunk.get("text") or "")[:2_400],
        },
    }
    safe_url = _safe_source_url(hit.get("url"))
    if safe_url:
        result["url"] = safe_url
    return result


async def list_plugin_sources(*, session: Any, actor_user_id: UUID) -> list[dict[str, Any]]:
    """List only sources readable under normal source and Project ACLs."""

    sources = await KnowledgeService.list_sources(
        session,
        actor_user_id=actor_user_id,
        is_admin=False,
    )
    return [_source_dto(source) for source in sources]


async def search_plugin_knowledge(
    *,
    session: Any,
    actor_user_id: UUID,
    query: str,
    source_id: UUID | None,
    project_id: UUID | None,
    tags: list[str],
    extension: str | None,
    path_prefix: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Search through KnowledgeService and serialize only allowlisted fields."""

    bounded_limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
    hits = await KnowledgeService.search(
        session,
        query=query,
        actor_user_id=actor_user_id,
        is_admin=False,
        filters=KnowledgeSearchFilters(
            source_id=source_id,
            project_id=project_id,
            tags=tuple(tags),
            extension=extension,
            path_prefix=path_prefix,
        ),
        limit=bounded_limit,
    )
    return [
        projected
        for hit in hits[:bounded_limit]
        if (projected := _bounded_search_hit(hit)) is not None
    ]


async def read_plugin_document(
    *,
    session: Any,
    actor_user_id: UUID,
    document_id: UUID,
    view: str,
    max_chars: int = DEFAULT_READ_CHARS,
    offset: int = 0,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Return a bounded current-source page with a content-derived fingerprint."""

    if view not in {"body", "outline"}:
        raise ValueError("view must be body or outline")
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_READ_CHARS:
        raise ValueError(f"max_chars must be between 1 and {MAX_READ_CHARS}")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be non-negative")
    if offset > 0 and not fingerprint:
        raise ValueError("fingerprint is required when offset is greater than zero")

    try:
        payload = await KnowledgeService.read_document(
            session,
            actor_user_id=actor_user_id,
            is_admin=False,
            document_id=document_id,
        )
    except (PermissionError, LookupError, ValueError) as exc:
        raise KnowledgePluginNotFound from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise KnowledgePluginNotFound
    content = payload.get("content")
    if not isinstance(content, str):
        raise KnowledgePluginNotFound

    actual_fingerprint = hashlib.sha256(
        content.encode("utf-8", errors="replace")
    ).hexdigest()
    if fingerprint is not None and fingerprint != actual_fingerprint:
        raise KnowledgePluginContentChanged("Document changed; restart the read at offset 0")

    end = min(len(content), offset + max_chars)
    page = content[offset:end]
    result: dict[str, Any] = {
        "source": _source_dto(payload.get("source") or {}),
        "document": _document_dto(payload.get("document") or {}),
        "view": view,
        "content_fingerprint": actual_fingerprint,
        "offset": offset,
        "max_chars": max_chars,
        "total_chars": len(content),
        "has_more": end < len(content),
    }
    safe_url = _safe_source_url(payload.get("url"))
    if safe_url:
        result["url"] = safe_url
    if view == "body":
        result["content"] = page
    else:
        line_number = content.count("\n", 0, offset) + 1
        first_line_is_partial = (
            0 < offset <= len(content) and content[offset - 1] != "\n"
        )
        headings = []
        for index, line in enumerate(page.splitlines()):
            match = _HEADING_RE.match(line)
            if match and not (index == 0 and first_line_is_partial):
                headings.append(
                    {
                        "level": len(match.group(1)),
                        "title": match.group(2).strip()[:1000],
                        "line": line_number,
                    }
                )
            line_number += 1
        result["outline"] = headings
    return result


__all__ = [
    "DEFAULT_READ_CHARS",
    "MAX_READ_CHARS",
    "MAX_SEARCH_RESULTS",
    "KnowledgePluginContentChanged",
    "KnowledgePluginNotFound",
    "list_plugin_sources",
    "read_plugin_document",
    "search_plugin_knowledge",
]
