"""Knowledge Workspace derived search index.

The canonical data lives in project workspace files and Knowledge DB rows.
This module maintains the rebuildable Qdrant index used for hybrid retrieval.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import KnowledgeChunk, KnowledgeDocument, KnowledgeSource
from ..rag.config import RagConfig, get_rag_config
from ..rag.embedding import BgeM3Embedding
from ..rag.qdrant_client import SharedQdrantClient

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models

    QDRANT_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    QDRANT_AVAILABLE = False
    QdrantClient = None  # type: ignore[assignment]
    models = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


def _knowledge_search_enabled() -> bool:
    """ナレッジRAGのマスタースイッチ (`search.knowledge_enabled`, 既定OFF)。"""
    try:
        from ..config import Config

        search = Config().config.get("search", {})
        if not isinstance(search, dict):
            return False
        return bool(search.get("knowledge_enabled", False))
    except Exception:
        return False


_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/#-]*")
_CJK_RUN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]+")


@dataclass(frozen=True)
class KnowledgeIndexHit:
    chunk_id: uuid.UUID
    score: float


@dataclass(frozen=True)
class KnowledgeIndexSyncResult:
    status: str
    indexed_chunks: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "indexed_chunks": self.indexed_chunks,
            "error": self.error,
        }


class HashingSparseEncoder:
    """Small deterministic sparse encoder for exact/lexical retrieval.

    BGE-M3 dense embeddings cover semantic similarity. This sparse side keeps
    filenames, IDs, model numbers, IP addresses, and Japanese character n-grams
    searchable without requiring a separate sparse model runtime.
    """

    def encode(self, text: str):
        if models is None:
            raise RuntimeError("qdrant-client is not available")
        weights: dict[int, float] = {}
        for token in self._tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
            index = int.from_bytes(digest, "big") & 0x7FFFFFFF
            if index == 0:
                index = 1
            weights[index] = weights.get(index, 0.0) + 1.0
        if not weights:
            return models.SparseVector(indices=[], values=[])
        norm = math.sqrt(sum(value * value for value in weights.values())) or 1.0
        items = sorted(weights.items())
        return models.SparseVector(
            indices=[index for index, _ in items],
            values=[value / norm for _, value in items],
        )

    def _tokens(self, text: str) -> list[str]:
        lowered = (text or "").casefold()
        tokens: list[str] = []
        tokens.extend(match.group(0) for match in _ASCII_TOKEN_RE.finditer(lowered))
        for run in _CJK_RUN_RE.findall(lowered):
            tokens.append(run)
            for size in (2, 3):
                if len(run) >= size:
                    tokens.extend(run[index : index + size] for index in range(len(run) - size + 1))
        return [token for token in tokens if token]


class KnowledgeIndexService:
    """Synchronize and query the derived Qdrant Knowledge index."""

    dense_vector_name = "dense"
    sparse_vector_name = "sparse"

    def __init__(self, config: Optional[RagConfig] = None) -> None:
        self.config = config or get_rag_config()
        self.collection_name = self.config.qdrant.collection_name
        self.client: Optional[QdrantClient] = None
        self.embedding = BgeM3Embedding(self.config.embedding)
        self.sparse_encoder = HashingSparseEncoder()
        self._initialized = False
        self._is_local_mode = False

    async def initialize(self) -> bool:
        if self._initialized:
            return True
        if not _knowledge_search_enabled():
            logger.info(
                "Knowledge index is disabled (search.knowledge_enabled is off)"
            )
            return False
        if not QDRANT_AVAILABLE:
            logger.warning("qdrant-client is not installed; Knowledge index disabled")
            return False
        if not await self.embedding.initialize():
            logger.warning("Embedding model could not be initialized; Knowledge index disabled")
            return False

        try:
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
        except Exception as exc:
            logger.exception("Failed to initialize Knowledge index")
            return False

    def _ensure_collection(self) -> None:
        if self.client is None or models is None:
            raise RuntimeError("Qdrant client is not initialized")

        collections = self.client.get_collections()
        names = {collection.name for collection in collections.collections}
        if self.collection_name in names and not self._collection_is_compatible():
            logger.warning(
                "Recreating incompatible Knowledge index collection %s; index data is derived",
                self.collection_name,
            )
            self.client.delete_collection(self.collection_name)
            names.remove(self.collection_name)

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

        if not self._is_local_mode:
            for field in ("source_id", "document_id", "chunk_id", "project_refs", "task_refs", "tags", "extension"):
                try:
                    self.client.create_payload_index(
                        collection_name=self.collection_name,
                        field_name=field,
                        field_schema=models.PayloadSchemaType.KEYWORD,
                    )
                except Exception:
                    logger.debug("Payload index already exists or could not be created: %s", field)

    def _collection_is_compatible(self) -> bool:
        if self.client is None:
            return False
        try:
            info = self.client.get_collection(self.collection_name)
            params = info.config.params
            vectors = getattr(params, "vectors", None)
            sparse_vectors = getattr(params, "sparse_vectors", None)
            has_dense = isinstance(vectors, dict) and self.dense_vector_name in vectors
            has_sparse = isinstance(sparse_vectors, dict) and self.sparse_vector_name in sparse_vectors
            return bool(has_dense and has_sparse)
        except Exception:
            logger.debug("Could not inspect Knowledge index collection", exc_info=True)
            return False

    async def sync_source(self, session: AsyncSession, source_id: uuid.UUID) -> KnowledgeIndexSyncResult:
        if not await self.initialize():
            return KnowledgeIndexSyncResult(status="disabled")
        if self.client is None or models is None:
            return KnowledgeIndexSyncResult(status="unavailable")

        rows = await session.execute(
            select(KnowledgeChunk, KnowledgeDocument, KnowledgeSource)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .join(KnowledgeSource, KnowledgeDocument.source_id == KnowledgeSource.id)
            .where(
                KnowledgeDocument.source_id == source_id,
                KnowledgeDocument.status == "active",
            )
            .order_by(KnowledgeDocument.path.asc(), KnowledgeChunk.chunk_index.asc())
        )
        chunk_rows = list(rows.all())
        await asyncio.to_thread(self._delete_source_points, source_id)
        if not chunk_rows:
            return KnowledgeIndexSyncResult(status="synced", indexed_chunks=0)

        texts = [chunk.text for chunk, _, _ in chunk_rows]
        dense_vectors = await self.embedding.embed(texts)
        if len(dense_vectors) != len(chunk_rows):
            return KnowledgeIndexSyncResult(
                status="error",
                error="embedding vector count did not match Knowledge chunks",
            )

        points = []
        for dense, (chunk, document, source) in zip(dense_vectors, chunk_rows):
            vector_id = str(chunk.id)
            chunk.vector_id = vector_id
            points.append(
                models.PointStruct(
                    id=vector_id,
                    vector={
                        self.dense_vector_name: dense,
                        self.sparse_vector_name: self.sparse_encoder.encode(
                            self._sparse_text(chunk, document, source)
                        ),
                    },
                    payload=self._payload(chunk, document, source),
                )
            )

        for index in range(0, len(points), self.config.indexing.batch_size):
            batch = points[index : index + self.config.indexing.batch_size]
            await asyncio.to_thread(
                self.client.upsert,
                collection_name=self.collection_name,
                points=batch,
            )
        return KnowledgeIndexSyncResult(status="synced", indexed_chunks=len(points))

    async def search(
        self,
        *,
        query: str,
        filters: Any,
        limit: int,
    ) -> list[KnowledgeIndexHit]:
        query = query.strip()
        if not query or not await self.initialize():
            return []
        if self.client is None or models is None:
            return []

        dense = await self.embedding.embed_query(query)
        sparse = self.sparse_encoder.encode(query)
        query_filter = self._build_filter(filters)
        search_limit = max(limit, 1)
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

        try:
            if len(prefetch) == 1:
                response = await asyncio.to_thread(
                    self.client.query_points,
                    collection_name=self.collection_name,
                    query=prefetch[0].query,
                    using=prefetch[0].using,
                    query_filter=query_filter,
                    limit=search_limit,
                    with_payload=["chunk_id"],
                )
            else:
                response = await asyncio.to_thread(
                    self.client.query_points,
                    collection_name=self.collection_name,
                    prefetch=prefetch,
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    query_filter=query_filter,
                    limit=search_limit,
                    with_payload=["chunk_id"],
                )
        except Exception:
            logger.exception("Knowledge index search failed")
            return []

        hits: list[KnowledgeIndexHit] = []
        for point in response.points:
            payload = point.payload or {}
            chunk_id = payload.get("chunk_id")
            if not chunk_id:
                continue
            try:
                hits.append(KnowledgeIndexHit(chunk_id=uuid.UUID(str(chunk_id)), score=float(point.score)))
            except ValueError:
                continue
        return hits

    def _delete_source_points(self, source_id: uuid.UUID) -> None:
        if self.client is None or models is None:
            return
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source_id",
                            match=models.MatchValue(value=str(source_id)),
                        )
                    ]
                )
            ),
        )

    def _build_filter(self, filters: Any):
        if models is None:
            return None
        must = [
            models.FieldCondition(
                key="document_status",
                match=models.MatchValue(value="active"),
            )
        ]
        if getattr(filters, "source_id", None):
            must.append(
                models.FieldCondition(
                    key="source_id",
                    match=models.MatchValue(value=str(filters.source_id)),
                )
            )
        if getattr(filters, "project_id", None):
            must.append(
                models.FieldCondition(
                    key="project_refs",
                    match=models.MatchValue(value=str(filters.project_id)),
                )
            )
        if getattr(filters, "extension", None):
            extension = str(filters.extension)
            if not extension.startswith("."):
                extension = f".{extension}"
            must.append(
                models.FieldCondition(
                    key="extension",
                    match=models.MatchValue(value=extension.lower()),
                )
            )
        for tag in getattr(filters, "tags", ()) or ():
            must.append(
                models.FieldCondition(
                    key="tags",
                    match=models.MatchValue(value=str(tag)),
                )
            )
        return models.Filter(must=must)

    def _payload(
        self,
        chunk: KnowledgeChunk,
        document: KnowledgeDocument,
        source: KnowledgeSource,
    ) -> dict[str, Any]:
        return {
            "text": chunk.text,
            "chunk_id": str(chunk.id),
            "document_id": str(document.id),
            "source_id": str(source.id),
            "source_name": source.name,
            "source_type": source.source_type,
            "document_status": document.status,
            "title": document.title,
            "path": document.path,
            "extension": document.extension,
            "heading_path": chunk.heading_path or [],
            "chunk_index": chunk.chunk_index,
            "content_hash": chunk.content_hash,
            "tags": document.tags or [],
            "project_refs": [str(ref) for ref in document.project_refs or []],
            "task_refs": [str(ref) for ref in document.task_refs or []],
        }

    def _sparse_text(
        self,
        chunk: KnowledgeChunk,
        document: KnowledgeDocument,
        source: KnowledgeSource,
    ) -> str:
        return "\n".join(
            [
                source.name or "",
                document.title or "",
                document.path or "",
                " ".join(document.tags or []),
                " ".join(str(ref) for ref in document.project_refs or []),
                " ".join(str(ref) for ref in document.task_refs or []),
                " > ".join(chunk.heading_path or []),
                chunk.text,
            ]
        )


_knowledge_index_service: Optional[KnowledgeIndexService] = None


def get_knowledge_index_service() -> KnowledgeIndexService:
    global _knowledge_index_service
    if _knowledge_index_service is None:
        _knowledge_index_service = KnowledgeIndexService()
    return _knowledge_index_service
