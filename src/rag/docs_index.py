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
- A per-node content hash (over the embedding input) lets ``reconcile_workspace``
  skip unchanged nodes and re-embed only what changed — including when a parent
  rename changes a descendant's ancestor path.
- Frontend Docs edits write straight to Postgres (bypassing the Python service),
  so ``reconcile_workspace`` is the catch-all sync; ``enqueue_docs_reindex`` gives
  near-real-time updates for backend/agent-originated edits.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
)
from .config import RagConfig, get_rag_config
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


def docs_rag_enabled(config: Optional[RagConfig] = None) -> bool:
    cfg = config or get_rag_config()
    return bool(getattr(cfg, "docs_enabled", False)) and QDRANT_AVAILABLE


def _node_text(title: str, path_titles: list[str], tags: list[str], description: str = "") -> str:
    """Build the contextual text embedded for a node."""
    parts: list[str] = []
    path = " / ".join(t for t in path_titles if t)
    if path:
        parts.append(f"path: {path}")
    if tags:
        parts.append("tags: " + " ".join(f"#{t}" for t in tags))
    parts.append(str(title or "").strip())
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

    async def initialize(self) -> bool:
        if self._initialized:
            return True
        if self._disabled or not docs_rag_enabled(self.config):
            return False
        try:
            if not await self.embedding.initialize():
                logger.warning("Docs index: embedding model unavailable; disabling for this process")
                self._disabled = True
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
            return True
        except Exception:
            logger.exception("Docs index: failed to initialize; disabling for this process")
            self._disabled = True
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

    async def _delete_ids(self, node_ids: list[uuid.UUID]) -> None:
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
        return models.PointStruct(
            id=str(node.id),
            vector={
                self.dense_vector_name: dense,
                self.sparse_vector_name: self.sparse_encoder.encode(text),
            },
            payload={
                "node_id": str(node.id),
                "workspace_id": str(node.workspace_id),
                "project_id": str(node.project_id) if node.project_id else "",
                "title": node.title or "",
                "tags": tags,
                "text": text,
                "content_hash": _content_hash(text),
            },
        )

    async def _existing_hashes(self, workspace_id: uuid.UUID) -> dict[str, str]:
        """Return node_id -> content_hash for points already in the collection."""
        if self.client is None or models is None:
            return {}
        hashes: dict[str, str] = {}
        next_offset = None
        flt = models.Filter(
            must=[
                models.FieldCondition(
                    key="workspace_id",
                    match=models.MatchValue(value=str(workspace_id)),
                )
            ]
        )
        while True:
            records, next_offset = await asyncio.to_thread(
                self.client.scroll,
                collection_name=self.collection_name,
                scroll_filter=flt,
                limit=2000,
                with_payload=["node_id", "content_hash"],
                with_vectors=False,
                offset=next_offset,
            )
            for record in records:
                payload = record.payload or {}
                node_id = payload.get("node_id")
                if node_id:
                    hashes[str(node_id)] = str(payload.get("content_hash") or "")
            if next_offset is None:
                break
        return hashes

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

    async def reconcile_workspace(self, session: AsyncSession, workspace_id: uuid.UUID) -> dict:
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
            ).where(
                KnowledgeNode.workspace_id == workspace_id,
                KnowledgeNode.archived_at.is_(None),
            )
        )
        node_rows = list(rows.all())
        title_by_id = {row.id: (row.title or "") for row in node_rows}
        parent_by_id = {row.id: row.parent_id for row in node_rows}

        tag_rows = await session.execute(
            select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
            .join(KnowledgeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(KnowledgeNodeSupertag.node_id.in_(list(title_by_id.keys()) or [uuid.uuid4()]))
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

        existing = await self._existing_hashes(workspace_id)
        current_ids: set[str] = set()
        to_index: list[tuple[KnowledgeNode, str, list[str]]] = []
        for row in node_rows:
            current_ids.add(str(row.id))
            tags = tags_by_id.get(row.id, [])
            text = _node_text(row.title or "", ancestor_titles(row.id), tags, row.description or "")
            if existing.get(str(row.id)) == _content_hash(text):
                continue
            node = KnowledgeNode(
                id=row.id,
                workspace_id=workspace_id,
                title=row.title,
                project_id=row.project_id,
            )
            to_index.append((node, text, tags))

        indexed = await self._index_records(to_index)

        stale_ids = [uuid.UUID(nid) for nid in existing.keys() if nid not in current_ids]
        await self._delete_ids(stale_ids)

        return {
            "status": "synced",
            "total": len(node_rows),
            "reindexed": indexed,
            "removed": len(stale_ids),
        }

    async def index_node_ids(
        self, session: AsyncSession, workspace_id: uuid.UUID, node_ids: list[uuid.UUID]
    ) -> int:
        """Targeted (re)index for specific nodes (agent/backend-originated edits)."""
        if not node_ids or not await self.initialize():
            return 0
        from ..services.docs_graph_service import DocsGraphService

        service = DocsGraphService(session)
        records: list[tuple[KnowledgeNode, str, list[str]]] = []
        remove: list[uuid.UUID] = []
        for node_id in node_ids:
            node = await session.get(KnowledgeNode, node_id)
            if node is None or node.workspace_id != workspace_id or node.archived_at is not None:
                remove.append(node_id)
                continue
            ancestors = await service.ancestor_titles(node)
            tag_rows = await session.execute(
                select(KnowledgeSupertag.name)
                .join(KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
                .where(KnowledgeNodeSupertag.node_id == node.id)
            )
            tags = [name for (name,) in tag_rows.all()]
            text = _node_text(node.title or "", ancestors, tags, node.description or "")
            records.append((node, text, tags))
        await self._delete_ids(remove)
        return await self._index_records(records)

    # -- search -------------------------------------------------------------

    async def search(
        self,
        *,
        workspace_id: uuid.UUID,
        query: str,
        project_id: Optional[uuid.UUID] = None,
        limit: int = 20,
    ) -> list[uuid.UUID]:
        query = str(query or "").strip()
        if not query or not await self.initialize():
            return []
        if self.client is None or models is None:
            return []

        dense = await self.embedding.embed_query(query)
        sparse = self.sparse_encoder.encode(query)
        must = [
            models.FieldCondition(
                key="workspace_id", match=models.MatchValue(value=str(workspace_id))
            )
        ]
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
            return []

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
            return []

        hits: list[uuid.UUID] = []
        for point in response.points:
            node_id = (point.payload or {}).get("node_id")
            if not node_id:
                continue
            try:
                hits.append(uuid.UUID(str(node_id)))
            except ValueError:
                continue
        return hits


class _NullAsyncGuard:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


_NULL_GUARD = _NullAsyncGuard()

_docs_index_service: Optional[DocsIndexService] = None
_dirty: set[tuple[uuid.UUID, uuid.UUID]] = set()
_worker_started = False


def get_docs_index_service() -> DocsIndexService:
    global _docs_index_service
    if _docs_index_service is None:
        _docs_index_service = DocsIndexService()
    return _docs_index_service


async def search_docs_index(
    *,
    workspace_id: uuid.UUID,
    query: str,
    project_id: Optional[uuid.UUID] = None,
    limit: int = 20,
) -> list[uuid.UUID]:
    """Entry point used by ``docs_search``. Returns [] when disabled/unavailable."""
    if not docs_rag_enabled():
        return []
    try:
        return await get_docs_index_service().search(
            workspace_id=workspace_id,
            query=query,
            project_id=project_id,
            limit=limit,
        )
    except Exception:
        logger.debug("search_docs_index failed", exc_info=True)
        return []


def enqueue_docs_reindex(workspace_id: uuid.UUID, node_id: uuid.UUID) -> None:
    """Mark a node dirty and (best-effort) drain it in the background.

    Called from inside a Docs mutation transaction, so it must never raise and
    must be a cheap no-op when the Docs index is disabled.
    """
    if not docs_rag_enabled():
        return
    _dirty.add((workspace_id, node_id))
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    global _worker_started
    if not _worker_started:
        _worker_started = True
        loop.create_task(_drain_worker())


async def _drain_worker() -> None:
    global _worker_started
    try:
        await asyncio.sleep(2.0)  # debounce a burst of edits
        pending = list(_dirty)
        _dirty.clear()
        if not pending:
            return
        by_workspace: dict[uuid.UUID, list[uuid.UUID]] = {}
        for workspace_id, node_id in pending:
            by_workspace.setdefault(workspace_id, []).append(node_id)
        from ..memory.database import get_database_manager

        db = get_database_manager()
        service = get_docs_index_service()
        for workspace_id, node_ids in by_workspace.items():
            session = await db.get_session()
            try:
                await service.index_node_ids(session, workspace_id, node_ids)
            except Exception:
                logger.debug("Docs reindex worker failed", exc_info=True)
            finally:
                await session.close()
    finally:
        _worker_started = False
