"""Exhaustive lexical retrieval with bounded client-side working memory."""

from __future__ import annotations

import heapq
import re
import uuid
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import KnowledgeChunk, KnowledgeDocument, KnowledgeSource

if TYPE_CHECKING:
    from .service import KnowledgeSearchFilters


STREAM_BATCH_SIZE = 128
ACL_CACHE_SIZE = 128


def _payload(
    *,
    source: KnowledgeSource,
    document: KnowledgeDocument,
    chunk: KnowledgeChunk,
    text: str,
    score: float,
    url: str | None,
) -> dict[str, Any]:
    """Serialize a winner without reading its encrypted text property again."""
    return {
        "score": score,
        "retrieval": "lexical",
        "url": url,
        "source": source.to_dict(),
        "document": document.to_dict(),
        "chunk": {
            "id": str(chunk.id),
            "heading_path": chunk.heading_path or [],
            "chunk_index": chunk.chunk_index,
            "text": text,
        },
    }


async def search_lexical(
    session: AsyncSession,
    *,
    query: str,
    actor_user_id: uuid.UUID | None,
    is_admin: bool = False,
    filters: KnowledgeSearchFilters | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Scan all eligible chunks, retaining only the best ``limit`` payloads.

    Memory grows with the batch, result limit and bounded ACL cache, rather
    than corpus size. Full-text I/O and scoring remain exhaustive for nonempty
    queries. The caller owns the session/transaction; only our cursor is closed.
    """
    from .service import KnowledgeSearchFilters, KnowledgeService

    if limit <= 0:
        return []
    filters = filters or KnowledgeSearchFilters()
    query = query.strip()
    terms = [term.lower() for term in re.split(r"\s+", query) if term]
    source_id = KnowledgeService._coerce_uuid(filters.source_id)
    extension = filters.extension
    if extension:
        extension = (
            extension if extension.startswith(".") else f".{extension}"
        ).lower()
    prefix = (filters.path_prefix or "").lower()
    tags = frozenset(str(tag).lower() for tag in filters.tags or ())
    project_id = str(filters.project_id) if filters.project_id else None

    conditions = [KnowledgeDocument.status == "active"]
    if source_id is not None:
        conditions.append(KnowledgeDocument.source_id == source_id)
    if extension:
        conditions.append(KnowledgeDocument.extension == extension)
    if prefix:
        conditions.append(
            func.lower(KnowledgeDocument.path).startswith(prefix, autoescape=True)
        )
    statement = (
        select(KnowledgeChunk, KnowledgeDocument, KnowledgeSource)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .join(KnowledgeSource, KnowledgeDocument.source_id == KnowledgeSource.id)
        .where(*conditions)
        .order_by(
            KnowledgeDocument.updated_at.desc().nullslast(),
            KnowledgeDocument.id.asc(),
            KnowledgeChunk.chunk_index.asc(),
            KnowledgeChunk.id.asc(),
        )
    )
    if not query:
        # Preserve the bounded recent-candidate behavior for empty queries.
        statement = statement.limit(max(limit * 5, limit))

    acl_cache: OrderedDict[uuid.UUID, bool] = OrderedDict()
    winners: list[tuple[float, int, dict[str, Any]]] = []
    result = await session.stream(
        statement.execution_options(yield_per=STREAM_BATCH_SIZE)
    )
    sequence = 0
    try:
        async for chunk, document, source in result:
            sequence += 1
            # Recheck current DB metadata before touching the plaintext getter.
            if document.status != "active" or document.source_id != source.id:
                continue
            if source_id is not None and document.source_id != source_id:
                continue
            if extension and document.extension != extension:
                continue
            if prefix and not (document.path or "").lower().startswith(prefix):
                continue
            if tags and not tags.issubset(
                str(tag).lower() for tag in document.tags or ()
            ):
                continue
            if project_id and project_id not in {
                str(ref) for ref in document.project_refs or ()
            }:
                continue

            if source.id in acl_cache:
                allowed = acl_cache[source.id]
                acl_cache.move_to_end(source.id)
            else:
                allowed = await KnowledgeService.can_read_source(
                    session,
                    source_id=source.id,
                    actor_user_id=actor_user_id,
                    is_admin=is_admin,
                )
                if len(acl_cache) == ACL_CACHE_SIZE:
                    acl_cache.popitem(last=False)
                acl_cache[source.id] = allowed
            if not allowed:
                continue

            text = chunk.text or ""
            if terms:
                haystack = "\n".join(
                    (text, document.title or "", document.path or "")
                ).lower()
                if not all(term in haystack for term in terms):
                    continue
            score = KnowledgeService._lexical_score(query, text, document)
            # Earlier rows win ties; sequence is unique, so heap comparisons
            # never reach the dictionaries. No payload is built for a loser.
            rank = (score, -sequence)
            if len(winners) == limit and rank <= winners[0][:2]:
                continue
            payload = _payload(
                source=source,
                document=document,
                chunk=chunk,
                text=text,
                score=score,
                url=KnowledgeService._document_url(source, document),
            )
            entry = (*rank, payload)
            if len(winners) < limit:
                heapq.heappush(winners, entry)
            else:
                heapq.heapreplace(winners, entry)
            if not query and len(winners) == limit:
                break
    finally:
        await result.close()

    return [
        entry[2] for entry in sorted(winners, key=lambda entry: entry[:2], reverse=True)
    ]
