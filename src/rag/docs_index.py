"""Semantic index for the AoiTalk Docs graph (``KnowledgeNode``).

The Docs graph stores content as an outliner: each node is one short claim and
its "body" is its child nodes. So the retrieval unit is a single node, embedded
together with a small amount of context (its ancestor path and tags) — the
Contextual Retrieval pattern applied to an outliner.

This index is derived and rebuildable. The canonical data is Postgres. Indexing
is opt-in via ``rag.docs_enabled`` (default off) so the default runtime does not
load an embedding model on every Docs edit. When disabled or unavailable,
``search_docs_index`` returns ``[]`` and ``docs_search`` falls back to the
lexical (DB) search.

Design notes:
- Collection ``rag.docs_collection_name`` (default ``aoitalk_docs``), separate
  from the Knowledge Workspace collection, with named dense + sparse vectors.
- Point id = ``str(node_id)`` so upserts replace in place.
- A per-node content hash (over the embedding input) lets ``reconcile_library``
  skip unchanged nodes and re-embed only what changed — including when a parent
  rename changes a descendant's ancestor path.
- Frontend Docs edits write straight to Postgres (bypassing the Python service),
  so ``reconcile_library`` is the catch-all sync; ``enqueue_docs_reindex`` gives
  near-real-time updates for backend/agent-originated edits.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    DocsLibrary,
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
)
from ..security.field_crypto import decrypt_json_value_if_needed, decrypt_text_if_needed
from ..services.docs_acl import docs_readable_node_predicate
from ..services.docs_graph_service import docs_searchable_body_text
from .config import RagConfig, get_rag_config
from .docs_search_telemetry import DocsIndexSearchTelemetry
from .embedding import BgeM3Embedding
from .qdrant_client import _LOCAL_QUERY_LOCK, SharedQdrantClient

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models

    QDRANT_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    QDRANT_AVAILABLE = False
    QdrantClient = None  # type: ignore[assignment]
    models = None  # type: ignore[assignment]

# Reuse the dependency-free sparse encoder (Japanese n-gram aware) from the
# Knowledge index so exact matches on tags / ids / model numbers stay findable.
from ..knowledge.index_service import HashingSparseEncoder

logger = logging.getLogger(__name__)


class DocsIndexUnavailable(RuntimeError):
    """Raised when targeted Docs indexing cannot reach the derived Qdrant index."""


REINDEX_INIT_RETRY_SECONDS = 30.0
REINDEX_WORKER_BACKOFF_INITIAL_SECONDS = 5.0
REINDEX_WORKER_BACKOFF_MAX_SECONDS = 60.0


def docs_rag_enabled(config: Optional[RagConfig] = None) -> bool:
    cfg = config or get_rag_config()
    return bool(getattr(cfg, "docs_enabled", False)) and QDRANT_AVAILABLE


def _node_text(
    title: str,
    path_titles: list[str],
    tags: list[str],
    description: str = "",
    body_text: str = "",
    body_json: object = None,
) -> str:
    """Build the contextual text embedded for a node.

    ``body_text`` is normally the title mirror and therefore does not need to
    be duplicated in the embedding input.  A typed Markdown/code block has
    independent editable content in ``body_json.content``; the shared helper
    selects that content so multiline source is available to both sparse and
    dense retrieval lanes.
    """
    parts: list[str] = []
    path = " / ".join(t for t in path_titles if t)
    if path:
        parts.append(f"path: {path}")
    if tags:
        parts.append("tags: " + " ".join(f"#{t}" for t in tags))
    title_text = str(title or "").strip()
    parts.append(title_text)
    body = docs_searchable_body_text(body_text, body_json).strip()
    if body and body != title_text:
        parts.append(body)
    if description:
        parts.append(str(description).strip())
    return "\n".join(p for p in parts if p)


def _content_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


class DocsIndexService:
    """Synchronize and query the derived Qdrant Docs index."""

    dense_vector_name = "dense"
    sparse_vector_name = "sparse"

    def __init__(self, config: Optional[RagConfig] = None) -> None:
        self.config = config or get_rag_config()
        self.collection_name = getattr(self.config, "docs_collection_name", "aoitalk_docs")
        self.client: Optional[QdrantClient] = None
        self.embedding = BgeM3Embedding(self.config.embedding)
        self.sparse_encoder = HashingSparseEncoder()
        self._initialized = False
        self._is_local_mode = False
        # Once initialization fails (no model, no Qdrant, GPU OOM, ...), stay off
        # for the rest of the process so search silently falls back to lexical
        # instead of retrying the heavy model load on every call.
        self._disabled = False
        self._disabled_at: float | None = None

    def _mark_disabled(self) -> None:
        self._disabled = True
        self._disabled_at = time.monotonic()

    def _reindex_retry_allowed(self) -> bool:
        if not self._disabled:
            return True
        if self._disabled_at is None:
            return True
        return (time.monotonic() - self._disabled_at) >= REINDEX_INIT_RETRY_SECONDS

    async def initialize(self) -> bool:
        """Search/read path: stay off after a failed init for this process."""
        if self._initialized:
            return True
        if self._disabled or not docs_rag_enabled(self.config):
            return False
        return await self._do_initialize()

    async def initialize_for_reindex(self) -> bool:
        """Reindex path: retry initialization after a bounded backoff."""
        if self._initialized:
            return True
        if not docs_rag_enabled(self.config):
            return False
        if self._disabled:
            if not self._reindex_retry_allowed():
                return False
            self._disabled = False
        return await self._do_initialize()

    async def _do_initialize(self) -> bool:
        try:
            if not await self.embedding.initialize():
                logger.warning(
                    "Docs index: embedding model unavailable; disabling for this process"
                )
                self._mark_disabled()
                return False
            if self.config.qdrant.local_path:
                self.client = SharedQdrantClient.get_client(self.config.qdrant.local_path)
                self._is_local_mode = True
            else:
                self.client = QdrantClient(
                    host=self.config.qdrant.host,
                    port=self.config.qdrant.port,
                    api_key=self.config.qdrant.api_key,
                )
                self._is_local_mode = False
            self._ensure_collection()
            self._initialized = True
            self._disabled = False
            self._disabled_at = None
            return True
        except Exception:
            logger.exception("Docs index: failed to initialize; disabling for this process")
            self._mark_disabled()
            return False

    def _ensure_collection(self) -> None:
        if self.client is None or models is None:
            raise RuntimeError("Qdrant client is not initialized")
        names = {c.name for c in self.client.get_collections().collections}
        if self.collection_name not in names:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    self.dense_vector_name: models.VectorParams(
                        size=self.embedding.dimension,
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    self.sparse_vector_name: models.SparseVectorParams(
                        index=models.SparseIndexParams(on_disk=False)
                    )
                },
            )

    # -- indexing -----------------------------------------------------------

    async def _upsert_points(self, points: list) -> None:
        if not points or self.client is None:
            return
        guard = _LOCAL_QUERY_LOCK if self._is_local_mode else _NULL_GUARD
        async with guard:
            await asyncio.to_thread(
                self.client.upsert,
                collection_name=self.collection_name,
                points=points,
            )

    async def _delete_ids(self, node_ids: list[uuid.UUID | str]) -> None:
        if not node_ids or self.client is None or models is None:
            return
        guard = _LOCAL_QUERY_LOCK if self._is_local_mode else _NULL_GUARD
        async with guard:
            await asyncio.to_thread(
                self.client.delete,
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(points=[str(nid) for nid in node_ids]),
            )

    def _build_point(self, node: KnowledgeNode, text: str, tags: list[str], dense: list[float]):
        docs_library_id = str(node.docs_library_id)
        return models.PointStruct(
            id=str(node.id),
            vector={
                self.dense_vector_name: dense,
                self.sparse_vector_name: self.sparse_encoder.encode(text),
            },
            payload={
                "node_id": str(node.id),
                "docs_library_id": docs_library_id,
                "project_id": str(node.project_id) if node.project_id else "",
                "title": node.title or "",
                "tags": tags,
                "text": text,
                "content_hash": _content_hash(text),
            },
        )

    @staticmethod
    def _library_filter(docs_library_id: uuid.UUID):
        """Scope Qdrant points to one canonical Docs Library."""

        value = str(docs_library_id)
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="docs_library_id", match=models.MatchValue(value=value)
                )
            ]
        )

    @staticmethod
    def _legacy_library_filter(docs_library_id: uuid.UUID):
        """Legacy ``workspace_id`` filter used only by reconciliation."""

        return models.FieldCondition(
            key="workspace_id", match=models.MatchValue(value=str(docs_library_id))
        )

    @classmethod
    def _library_scan_filter(cls, docs_library_id: uuid.UUID):
        """Scan canonical and pre-rename payloads at the reindex boundary.

        Search/query paths intentionally call :meth:`_library_filter` only;
        accepting ``workspace_id`` there would make an old payload a current
        API scope.  The reconciler is the one migration boundary allowed to
        dual-read and canonicalize those points.
        """

        return models.Filter(
            should=[
                cls._library_filter(docs_library_id).must[0],
                cls._legacy_library_filter(docs_library_id),
            ]
        )

    async def _existing_points(
        self, docs_library_id: uuid.UUID
    ) -> tuple[dict[str, str], set[str], dict[str, set[str]]]:
        """Return hashes, legacy node IDs, and Qdrant point IDs by node.

        ``workspace_id`` is intentionally observed only here, at the explicit
        migration/reindex boundary.  A point's payload node ID is preferred;
        for old points that omitted it, the Qdrant point ID remains a stale
        candidate and will be removed during reconciliation.
        """

        if self.client is None or models is None:
            return {}, set(), {}
        hashes: dict[str, str] = {}
        legacy_nodes: set[str] = set()
        point_ids_by_node: dict[str, set[str]] = {}
        next_offset = None
        flt = self._library_scan_filter(docs_library_id)
        while True:
            records, next_offset = await asyncio.to_thread(
                self.client.scroll,
                collection_name=self.collection_name,
                scroll_filter=flt,
                limit=2000,
                with_payload=[
                    "node_id",
                    "content_hash",
                    "docs_library_id",
                    "workspace_id",
                ],
                with_vectors=False,
                offset=next_offset,
            )
            for record in records:
                payload = record.payload or {}
                point_id = str(getattr(record, "id", "") or "")
                node_id = payload.get("node_id")
                node_key = str(node_id) if node_id else ""
                if node_key:
                    hashes[node_key] = str(payload.get("content_hash") or "")
                    point_ids_by_node.setdefault(node_key, set()).add(point_id)
                    if payload.get("workspace_id"):
                        legacy_nodes.add(node_key)
                elif point_id:
                    # Keep malformed points in a dedicated bucket so the
                    # caller can delete them as stale without coercing an
                    # arbitrary string into UUID.
                    point_ids_by_node.setdefault(f"__point__:{point_id}", set()).add(point_id)
            if next_offset is None:
                break
        return hashes, legacy_nodes, point_ids_by_node

    async def _existing_hashes(self, docs_library_id: uuid.UUID) -> dict[str, str]:
        """Return node_id -> content_hash for points already in the collection."""
        hashes, _legacy_nodes, _point_ids = await self._existing_points(docs_library_id)
        return hashes

    async def indexed_library_ids(self) -> set[uuid.UUID]:
        """Discover canonical/legacy library IDs present in Qdrant.

        This is intentionally a reindex-only operation.  It lets the
        reconciler clean points whose Postgres library/node was deleted even
        when no active DB node remains to seed the library loop.
        """

        if self.client is None or models is None:
            return set()
        ids: set[uuid.UUID] = set()
        next_offset = None
        while True:
            records, next_offset = await asyncio.to_thread(
                self.client.scroll,
                collection_name=self.collection_name,
                limit=2000,
                with_payload=["docs_library_id", "workspace_id"],
                with_vectors=False,
                offset=next_offset,
            )
            for record in records:
                payload = record.payload or {}
                for key in ("docs_library_id", "workspace_id"):
                    value = payload.get(key)
                    if not value:
                        continue
                    try:
                        ids.add(uuid.UUID(str(value)))
                    except (TypeError, ValueError, AttributeError):
                        continue
            if next_offset is None:
                break
        return ids

    async def _index_records(self, records: list[tuple[KnowledgeNode, str, list[str]]]) -> int:
        """Embed and upsert a batch of (node, text, tags)."""
        if not records:
            return 0
        texts = [text for _, text, _ in records]
        dense_vectors = await self.embedding.embed(texts)
        if len(dense_vectors) != len(records):
            logger.warning("Docs index: embedding count mismatch; skipping batch")
            return 0
        points = [
            self._build_point(node, text, tags, dense)
            for dense, (node, text, tags) in zip(dense_vectors, records)
        ]
        batch_size = max(1, int(self.config.indexing.batch_size))
        for i in range(0, len(points), batch_size):
            await self._upsert_points(points[i : i + batch_size])
        return len(points)

    async def reconcile_library(self, session: AsyncSession, docs_library_id: uuid.UUID) -> dict:
        """Bring the index in line with Postgres for one workspace.

        Re-embeds only nodes whose contextual text changed and removes points for
        archived/deleted nodes. Safe to run on a schedule; this is the catch-all
        sync for edits made through the frontend (which bypasses Python).
        """
        if not await self.initialize():
            return {"status": "disabled"}

        rows = await session.execute(
            select(
                KnowledgeNode.id,
                KnowledgeNode.parent_id,
                KnowledgeNode.title,
                KnowledgeNode.description,
                KnowledgeNode.project_id,
                KnowledgeNode._body_text.label("body_text"),
                KnowledgeNode._body_json.label("body_json"),
            ).where(
                KnowledgeNode.docs_library_id == docs_library_id,
                KnowledgeNode.archived_at.is_(None),
            )
        )
        node_rows = list(rows.all())
        title_by_id = {row.id: (row.title or "") for row in node_rows}
        parent_by_id = {row.id: row.parent_id for row in node_rows}

        tag_rows = await session.execute(
            select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
            .join(KnowledgeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(
                KnowledgeNodeSupertag.node_id.in_(list(title_by_id.keys()) or [uuid.uuid4()]),
                # A malformed cross-library relation must not inject a
                # foreign tag name into this library's embedding text.
                KnowledgeSupertag.docs_library_id == docs_library_id,
            )
        )
        tags_by_id: dict[uuid.UUID, list[str]] = {}
        for node_id, tag_name in tag_rows.all():
            tags_by_id.setdefault(node_id, []).append(tag_name)

        def ancestor_titles(node_id: uuid.UUID) -> list[str]:
            titles: list[str] = []
            seen = {node_id}
            current = parent_by_id.get(node_id)
            while current is not None and current not in seen and current in title_by_id:
                seen.add(current)
                titles.append(title_by_id[current])
                current = parent_by_id.get(current)
            return list(reversed(titles))

        existing, legacy_nodes, point_ids_by_node = await self._existing_points(docs_library_id)
        current_ids: set[str] = set()
        to_index: list[tuple[KnowledgeNode, str, list[str]]] = []
        for row in node_rows:
            current_ids.add(str(row.id))
            tags = tags_by_id.get(row.id, [])
            body_text = decrypt_text_if_needed(
                getattr(row, "body_text", None),
                aad="knowledge_nodes.body_text",
            ) or ""
            body_json = decrypt_json_value_if_needed(
                getattr(row, "body_json", None),
                aad="knowledge_nodes.body_json",
            )
            text = _node_text(
                row.title or "",
                ancestor_titles(row.id),
                tags,
                row.description or "",
                body_text,
                body_json,
            )
            node_point_ids = point_ids_by_node.get(str(row.id), set())
            # A legacy payload must be upserted even when its hash is current,
            # so the point gets the canonical ``docs_library_id`` key and no
            # old ``workspace_id`` field remains in the current DTO.
            if (
                existing.get(str(row.id)) == _content_hash(text)
                and str(row.id) not in legacy_nodes
                and node_point_ids.issubset({str(row.id)})
            ):
                continue
            node = KnowledgeNode(
                id=row.id,
                docs_library_id=docs_library_id,
                title=row.title,
                project_id=row.project_id,
            )
            to_index.append((node, text, tags))

        indexed = await self._index_records(to_index)

        stale_point_ids: set[str] = set()
        for node_key, point_ids in point_ids_by_node.items():
            if node_key.startswith("__point__:") or node_key not in current_ids:
                stale_point_ids.update(point_ids)
            elif node_key in current_ids and not point_ids.issubset({node_key}):
                # Duplicate/legacy point IDs for a live node are removed after
                # the canonical point is upserted.
                stale_point_ids.update(point_ids - {node_key})
        await self._delete_ids(sorted(stale_point_ids))

        return {
            "status": "synced",
            "total": len(node_rows),
            "reindexed": indexed,
            "removed": len(stale_point_ids),
        }

    async def index_node_ids(
        self,
        session: AsyncSession,
        docs_library_id: uuid.UUID | None = None,
        node_ids: list[uuid.UUID] | None = None,
    ) -> int:
        """Targeted (re)index for specific nodes (agent/backend-originated edits)."""
        if docs_library_id is None:
            return 0
        node_ids = node_ids or []
        if not node_ids:
            return 0
        if not await self.initialize_for_reindex():
            raise DocsIndexUnavailable("Docs semantic index is unavailable")
        from ..services.docs_graph_service import DocsGraphService

        service = DocsGraphService(session)
        records: list[tuple[KnowledgeNode, str, list[str]]] = []
        remove: list[uuid.UUID] = []
        for node_id in node_ids:
            node = await session.get(KnowledgeNode, node_id)
            if node is None or node.docs_library_id != docs_library_id or node.archived_at is not None:
                remove.append(node_id)
                continue
            ancestors = await service.ancestor_titles(node)
            tag_rows = await session.execute(
                select(KnowledgeSupertag.name)
                .join(KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
                .where(
                    KnowledgeNodeSupertag.node_id == node.id,
                    KnowledgeSupertag.docs_library_id == docs_library_id,
                )
            )
            tags = [name for (name,) in tag_rows.all()]
            text = _node_text(
                node.title or "",
                ancestors,
                tags,
                node.description or "",
                node.body_text or "",
                node.body_json,
            )
            records.append((node, text, tags))
        await self._delete_ids(remove)
        return await self._index_records(records)

    # -- search -------------------------------------------------------------

    @staticmethod
    def _empty_search_result(
        *,
        fallback_reason: str,
        latency_ms: float = 0.0,
        dense_used: bool = False,
        sparse_used: bool = False,
        fusion: str = "none",
    ) -> "DocsIndexSearchResult":
        return DocsIndexSearchResult(
            node_ids=[],
            telemetry=DocsIndexSearchTelemetry(
                dense_used=dense_used,
                sparse_used=sparse_used,
                fusion=fusion,
                candidate_count=0,
                latency_ms=latency_ms,
                fallback_reason=fallback_reason,
            ),
        )

    async def search(
        self,
        *,
        docs_library_id: uuid.UUID | None = None,
        query: str,
        project_id: Optional[uuid.UUID] = None,
        limit: int = 20,
        user_id: Optional[uuid.UUID] = None,
        session: Optional[AsyncSession] = None,
    ) -> "DocsIndexSearchResult":
        started = time.perf_counter()
        if docs_library_id is None:
            return self._empty_search_result(fallback_reason="missing_library")
        query = str(query or "").strip()
        if not query:
            return self._empty_search_result(fallback_reason="empty_query")
        if not await self.initialize():
            return self._empty_search_result(
                fallback_reason="index_unavailable",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        if self.client is None or models is None:
            return self._empty_search_result(
                fallback_reason="client_unavailable",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        dense = await self.embedding.embed_query(query)
        sparse = self.sparse_encoder.encode(query)
        dense_used = bool(dense)
        sparse_used = bool(sparse.indices)
        # Search is a current-runtime path: only canonical payloads are
        # addressable.  Legacy ``workspace_id`` payloads are dual-read and
        # canonicalized exclusively by ``reconcile_library``.
        must = [self._library_filter(docs_library_id)]
        if project_id is not None:
            must.append(
                models.FieldCondition(
                    key="project_id", match=models.MatchValue(value=str(project_id))
                )
            )
        query_filter = models.Filter(must=must)
        search_limit = max(1, int(limit))

        prefetch = []
        if dense:
            prefetch.append(
                models.Prefetch(
                    query=dense,
                    using=self.dense_vector_name,
                    filter=query_filter,
                    limit=max(search_limit * 2, search_limit),
                )
            )
        if sparse.indices:
            prefetch.append(
                models.Prefetch(
                    query=sparse,
                    using=self.sparse_vector_name,
                    filter=query_filter,
                    limit=max(search_limit * 2, search_limit),
                )
            )
        if not prefetch:
            return self._empty_search_result(
                fallback_reason="no_vectors",
                latency_ms=(time.perf_counter() - started) * 1000.0,
                dense_used=dense_used,
                sparse_used=sparse_used,
            )

        fusion = "rrf" if len(prefetch) > 1 else (
            "dense" if dense_used else "sparse"
        )
        guard = _LOCAL_QUERY_LOCK if self._is_local_mode else _NULL_GUARD
        try:
            async with guard:
                if len(prefetch) == 1:
                    response = await asyncio.to_thread(
                        self.client.query_points,
                        collection_name=self.collection_name,
                        query=prefetch[0].query,
                        using=prefetch[0].using,
                        query_filter=query_filter,
                        limit=search_limit,
                        with_payload=["node_id"],
                    )
                else:
                    response = await asyncio.to_thread(
                        self.client.query_points,
                        collection_name=self.collection_name,
                        prefetch=prefetch,
                        query=models.FusionQuery(fusion=models.Fusion.RRF),
                        query_filter=query_filter,
                        limit=search_limit,
                        with_payload=["node_id"],
                    )
        except Exception:
            logger.exception("Docs index search failed")
            return self._empty_search_result(
                fallback_reason="qdrant_error",
                latency_ms=(time.perf_counter() - started) * 1000.0,
                dense_used=dense_used,
                sparse_used=sparse_used,
                fusion=fusion,
            )

        hits: list[uuid.UUID] = []
        for point in response.points:
            node_id = (point.payload or {}).get("node_id")
            if not node_id:
                continue
            try:
                hits.append(uuid.UUID(str(node_id)))
            except ValueError:
                continue
        if user_id is not None and session is None:
            return self._empty_search_result(
                fallback_reason="acl_session_required",
                latency_ms=(time.perf_counter() - started) * 1000.0,
                dense_used=dense_used,
                sparse_used=sparse_used,
                fusion=fusion,
            )
        if user_id is not None and session is not None:
            try:
                library = await session.get(DocsLibrary, docs_library_id)
                visibility = docs_readable_node_predicate(
                    KnowledgeNode,
                    docs_library_id=docs_library_id,
                    user_id=user_id,
                    library_owner_id=getattr(library, "owner_user_id", None),
                )
                stmt = select(KnowledgeNode.id).where(
                    KnowledgeNode.id.in_(hits),
                    KnowledgeNode.docs_library_id == docs_library_id,
                    KnowledgeNode.archived_at.is_(None),
                    visibility,
                )
                if project_id is not None:
                    stmt = stmt.where(KnowledgeNode.project_id == project_id)
                hits = list((await session.execute(stmt)).scalars().all())
            except Exception:
                logger.debug("Docs index ACL filter failed", exc_info=True)
                return self._empty_search_result(
                    fallback_reason="acl_filter_failed",
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    dense_used=dense_used,
                    sparse_used=sparse_used,
                    fusion=fusion,
                )
        return DocsIndexSearchResult(
            node_ids=hits,
            telemetry=DocsIndexSearchTelemetry(
                dense_used=dense_used,
                sparse_used=sparse_used,
                fusion=fusion,
                candidate_count=len(hits),
                latency_ms=(time.perf_counter() - started) * 1000.0,
            ),
        )


@dataclass(frozen=True)
class DocsIndexSearchResult:
    node_ids: list[uuid.UUID]
    telemetry: DocsIndexSearchTelemetry


class _NullAsyncGuard:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


_NULL_GUARD = _NullAsyncGuard()


_docs_index_service: Optional[DocsIndexService] = None
_dirty: set[tuple[uuid.UUID, uuid.UUID]] = set()
_pending_without_loop: set[tuple[uuid.UUID, uuid.UUID]] = set()
_worker_started = False
_reindex_worker_backoff_seconds = REINDEX_WORKER_BACKOFF_INITIAL_SECONDS
_reindex_worker_backoff_until = 0.0


def _reset_reindex_worker_backoff() -> None:
    global _reindex_worker_backoff_seconds, _reindex_worker_backoff_until
    _reindex_worker_backoff_seconds = REINDEX_WORKER_BACKOFF_INITIAL_SECONDS
    _reindex_worker_backoff_until = 0.0


def _schedule_reindex_worker_backoff() -> None:
    global _reindex_worker_backoff_seconds, _reindex_worker_backoff_until
    _reindex_worker_backoff_until = time.monotonic() + _reindex_worker_backoff_seconds
    _reindex_worker_backoff_seconds = min(
        _reindex_worker_backoff_seconds * 2,
        REINDEX_WORKER_BACKOFF_MAX_SECONDS,
    )


async def _wait_reindex_worker_backoff() -> None:
    delay = _reindex_worker_backoff_until - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)


def get_docs_index_service() -> DocsIndexService:
    global _docs_index_service
    if _docs_index_service is None:
        _docs_index_service = DocsIndexService()
    return _docs_index_service


async def search_docs_index_with_telemetry(
    *,
    docs_library_id: uuid.UUID | None = None,
    query: str,
    project_id: Optional[uuid.UUID] = None,
    limit: int = 20,
    user_id: Optional[uuid.UUID] = None,
    session: Optional[AsyncSession] = None,
) -> DocsIndexSearchResult:
    """Entry point used by ``docs_search`` with semantic-lane telemetry."""
    if docs_library_id is None or not docs_rag_enabled():
        return DocsIndexService._empty_search_result(fallback_reason="disabled")
    try:
        return await get_docs_index_service().search(
            docs_library_id=docs_library_id,
            query=query,
            project_id=project_id,
            limit=limit,
            user_id=user_id,
            session=session,
        )
    except Exception:
        logger.debug("search_docs_index failed", exc_info=True)
        return DocsIndexService._empty_search_result(fallback_reason="search_failed")


async def search_docs_index(
    *,
    docs_library_id: uuid.UUID | None = None,
    query: str,
    project_id: Optional[uuid.UUID] = None,
    limit: int = 20,
    user_id: Optional[uuid.UUID] = None,
    session: Optional[AsyncSession] = None,
) -> list[uuid.UUID]:
    """Entry point used by ``docs_search``. Returns [] when disabled/unavailable."""
    result = await search_docs_index_with_telemetry(
        docs_library_id=docs_library_id,
        query=query,
        project_id=project_id,
        limit=limit,
        user_id=user_id,
        session=session,
    )
    return result.node_ids


def enqueue_docs_reindex(
    docs_library_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
) -> None:
    """Mark a node dirty and (best-effort) drain it in the background.

    Called from inside a Docs mutation transaction, so it must never raise and
    must be a cheap no-op when the Docs index is disabled.
    """
    if docs_library_id is None or node_id is None:
        return
    if not docs_rag_enabled():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _pending_without_loop.add((docs_library_id, node_id))
        return
    _dirty.add((docs_library_id, node_id))
    global _worker_started
    if not _worker_started:
        _worker_started = True
        asyncio.get_running_loop().create_task(_drain_worker())


async def flush_pending_docs_reindex() -> None:
    """Flush enqueue requests made outside an asyncio loop and drain dirty nodes."""
    if _pending_without_loop:
        _dirty.update(_pending_without_loop)
        _pending_without_loop.clear()
    global _worker_started
    if not _dirty or _worker_started:
        return
    _worker_started = True
    await _drain_worker()


async def _drain_docs_reindex_once() -> bool:
    if not _dirty:
        return False
    await _wait_reindex_worker_backoff()
    await asyncio.sleep(2.0)  # debounce a burst of edits
    if not _dirty:
        return False
    pending = list(_dirty)
    _dirty.clear()
    by_library: dict[uuid.UUID, list[uuid.UUID]] = {}
    for docs_library_id, node_id in pending:
        by_library.setdefault(docs_library_id, []).append(node_id)
    from ..memory.database import get_database_manager

    db = get_database_manager()
    service = get_docs_index_service()
    failed: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for docs_library_id, node_ids in by_library.items():
        session = await db.get_session()
        try:
            await service.index_node_ids(session, docs_library_id, node_ids)
        except DocsIndexUnavailable:
            logger.debug("Docs reindex worker deferred: index unavailable", exc_info=True)
            for node_id in node_ids:
                failed.add((docs_library_id, node_id))
        except Exception:
            logger.debug("Docs reindex worker failed", exc_info=True)
            for node_id in node_ids:
                failed.add((docs_library_id, node_id))
        finally:
            await session.close()
    if failed:
        _dirty.update(failed)
        _schedule_reindex_worker_backoff()
        return bool(_dirty)
    _reset_reindex_worker_backoff()
    return bool(_dirty)


async def _drain_worker() -> None:
    global _worker_started
    try:
        while await _drain_docs_reindex_once():
            continue
    finally:
        _worker_started = False
        if _dirty:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                _pending_without_loop.update(_dirty)
                return
            if not _worker_started:
                _worker_started = True
                loop.create_task(_drain_worker())
