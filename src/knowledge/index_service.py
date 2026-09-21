"""Knowledge Workspace derived search index.

The canonical data lives in project workspace files and Knowledge DB rows.
This module maintains the rebuildable Qdrant index used for hybrid retrieval.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import math
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
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

# Like the native runner lock, these locks work across sync-bridge event loops.
# This service is the sole Knowledge index writer within the runtime process.
_SYNC_LOCKS: dict[tuple[str, ...], tuple[Any, int]] = {}
_SYNC_LOCKS_GUARD = threading.Lock()
_LOCAL_INDEX_IO_LOCK = threading.RLock()


@asynccontextmanager
async def _index_lock(key: tuple[str, ...]):
    with _SYNC_LOCKS_GUARD:
        lock, references = _SYNC_LOCKS.get(key, (threading.Lock(), 0))
        _SYNC_LOCKS[key] = (lock, references + 1)
    acquired = False
    try:
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.005)
        acquired = True
        yield
    finally:
        if acquired:
            lock.release()
        with _SYNC_LOCKS_GUARD:
            _, references = _SYNC_LOCKS[key]
            if references == 1:
                del _SYNC_LOCKS[key]
            else:
                _SYNC_LOCKS[key] = (lock, references - 1)


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
    """Sole writer for the derived Knowledge collection; never auto-migrate it.

    Source syncs serialize across threads/loops in this runtime process. A
    deployment must route writes here through one process, not independent
    writers. Incompatible collections require an explicit operator migration.
    """

    dense_vector_name = "dense"
    sparse_vector_name = "sparse"
    dense_input_version = "knowledge-context-v1"

    def __init__(self, config: Optional[RagConfig] = None) -> None:
        self.config = config or get_rag_config()
        self.collection_name = self.config.qdrant.collection_name
        self.client: Optional[QdrantClient] = None
        self.embedding = BgeM3Embedding(self.config.embedding)
        self.sparse_encoder = HashingSparseEncoder()
        self._initialized = False
        self._is_local_mode = False
        self._initialization_failure: Optional[KnowledgeIndexSyncResult] = None

    async def initialize(self) -> bool:
        async with _index_lock((*self._lock_scope(), "initialize")):
            return await self._initialize()

    def _lock_scope(self) -> tuple[str, ...]:
        config = self.config.qdrant
        endpoint = (
            os.path.normcase(str(Path(config.local_path).resolve()))
            if config.local_path
            else f"{config.host.casefold()}:{config.port}"
        )
        return (endpoint, self.collection_name)

    async def _index_io(self, operation, *args, **kwargs):
        finished = threading.Event()

        def invoke():
            try:
                if self._is_local_mode:
                    # Embedded Qdrant must not query while another index operation
                    # mutates its arrays, even for a different source or event loop.
                    with _LOCAL_INDEX_IO_LOCK:
                        return operation(*args, **kwargs)
                return operation(*args, **kwargs)
            finally:
                finished.set()

        # The loop owns this bounded executor and shuts it down on teardown.
        # Use a Future, not a Task that asyncio.run's all-task cancellation can
        # mark done before its thread finishes. Preserve to_thread's context.
        worker = asyncio.get_running_loop().run_in_executor(
            None, contextvars.copy_context().run, invoke
        )

        def consume_exception(future):
            if not future.cancelled():
                future.exception()

        # Cancellation can win over an eventual I/O failure. Retrieve that
        # exception even if its delivery to the loop follows finished.set().
        worker.add_done_callback(consume_exception)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A thread cannot be cancelled: retain the source lock until any
            # in-flight write finishes, including during loop teardown and
            # repeated cancellation. Only the executing thread signals this.
            while not finished.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
            raise

    async def _initialize(self) -> bool:
        if self._initialized:
            return True
        if not _knowledge_search_enabled():
            self._initialization_failure = KnowledgeIndexSyncResult(status="disabled")
            logger.info(
                "Knowledge index is disabled (search.knowledge_enabled is off)"
            )
            return False
        if not QDRANT_AVAILABLE:
            self._initialization_failure = KnowledgeIndexSyncResult(
                status="unavailable", error="qdrant-client is not installed"
            )
            logger.warning(self._initialization_failure.error)
            return False

        try:
            if not await self.embedding.initialize():
                self._initialization_failure = KnowledgeIndexSyncResult(
                    status="unavailable", error="Knowledge embedding model could not be initialized"
                )
                logger.warning(self._initialization_failure.error)
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
            await self._index_io(self._ensure_collection)
            self._initialized = True
            self._initialization_failure = None
            return True
        except Exception as exc:
            self._initialization_failure = KnowledgeIndexSyncResult(
                status="error",
                error=f"Knowledge index initialization failed: {exc or type(exc).__name__}",
            )
            logger.exception("Failed to initialize Knowledge index")
            return False

    def _ensure_collection(self) -> None:
        if self.client is None or models is None:
            raise RuntimeError("Qdrant client is not initialized")

        collections = self.client.get_collections()
        names = {collection.name for collection in collections.collections}
        if self.collection_name in names and not self._collection_is_compatible():
            raise RuntimeError(
                f"Knowledge collection {self.collection_name!r} is incompatible "
                "or could not be inspected; explicit migration is required"
            )

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
            if not isinstance(vectors, dict) or not isinstance(sparse_vectors, dict):
                return False
            if (
                set(vectors) != {self.dense_vector_name}
                or set(sparse_vectors) != {self.sparse_vector_name}
            ):
                return False
            dense = vectors[self.dense_vector_name]
            return (
                getattr(dense, "size", None) == self.embedding.dimension
                and getattr(dense, "distance", None) == models.Distance.COSINE
                and getattr(dense, "multivector_config", None) is None
            )
        except Exception:
            logger.debug("Could not inspect Knowledge index collection", exc_info=True)
            return False

    async def sync_source(self, session: AsyncSession, source_id: uuid.UUID) -> KnowledgeIndexSyncResult:
        async with _index_lock((*self._lock_scope(), "source", str(source_id))):
            return await self._sync_source(session, source_id)

    async def _sync_source(self, session: AsyncSession, source_id: uuid.UUID) -> KnowledgeIndexSyncResult:
        if not await self.initialize():
            return self._initialization_failure or KnowledgeIndexSyncResult(
                status="unavailable", error="Knowledge index initialization failed"
            )
        if self.client is None or models is None:
            return KnowledgeIndexSyncResult(
                status="unavailable", error="Knowledge Qdrant client is not available"
            )

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
        if not chunk_rows:
            await self._index_io(self._delete_source_points, source_id)
            return KnowledgeIndexSyncResult(status="synced", indexed_chunks=0)

        existing = await self._index_io(self._source_points, source_id)
        payloads = [self._payload(*row) for row in chunk_rows]
        dense_vectors = []
        changed = []
        for index, ((chunk, _, _), payload) in enumerate(zip(chunk_rows, payloads)):
            previous = existing.get(str(chunk.id))
            old_payload = (previous.payload or {}) if previous is not None else {}
            vectors = previous.vector if previous is not None else None
            dense = vectors.get(self.dense_vector_name) if isinstance(vectors, dict) else None
            if (
                old_payload.get("content_hash") != payload["content_hash"]
                or old_payload.get("embedding_fingerprint") != payload["embedding_fingerprint"]
                or not self._valid_dense_vector(dense)
            ):
                dense = None
                changed.append(index)
            dense_vectors.append(dense)

        generated = []
        if changed:
            generated = await self.embedding.embed(
                [self._dense_text(*chunk_rows[index]) for index in changed]
            )
        if len(generated) != len(changed) or any(
            not self._valid_dense_vector(vector) for vector in generated
        ):
            return KnowledgeIndexSyncResult(
                status="error",
                error="invalid Knowledge embeddings (count, dimension or finite values)",
            )
        for index, dense in zip(changed, generated):
            dense_vectors[index] = dense

        points = []
        for dense, (chunk, document, source), payload in zip(dense_vectors, chunk_rows, payloads):
            vector_id = str(chunk.id)
            points.append(
                models.PointStruct(
                    id=vector_id,
                    vector={
                        self.dense_vector_name: dense,
                        self.sparse_vector_name: self.sparse_encoder.encode(
                            self._sparse_text(chunk, document, source)
                        ),
                    },
                    payload=payload,
                )
            )

        batch_size = max(1, self.config.indexing.batch_size)
        for index in range(0, len(points), batch_size):
            batch = points[index : index + batch_size]
            # Upsert replaces the entire payload, clearing legacy plaintext
            # text even when we reuse the dense vector. Sparse is refreshed too.
            await self._index_io(
                self.client.upsert,
                collection_name=self.collection_name,
                points=batch,
                wait=True,
            )
        retained = {str(chunk.id) for chunk, _, _ in chunk_rows}
        stale = sorted(set(existing) - retained)
        for index in range(0, len(stale), batch_size):
            await self._index_io(
                self._delete_source_points, source_id, stale[index:index + batch_size]
            )
        for chunk, _, _ in chunk_rows:
            chunk.vector_id = str(chunk.id)
        return KnowledgeIndexSyncResult(status="synced", indexed_chunks=len(points))

    def _valid_dense_vector(self, vector: Any) -> bool:
        return (
            isinstance(vector, list)
            and len(vector) == self.embedding.dimension
            and all(
                isinstance(value, (int, float)) and math.isfinite(value)
                for value in vector
            )
            and any(value != 0 for value in vector)
        )

    def _source_points(self, source_id: uuid.UUID) -> dict[str, Any]:
        points = {}
        offset = None
        while True:
            records, offset = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=self._source_filter(source_id),
                with_payload=True,
                with_vectors=[self.dense_vector_name],
                limit=max(1, self.config.indexing.batch_size),
                offset=offset,
            )
            points.update((str(point.id), point) for point in records)
            if offset is None:
                return points

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
                response = await self._index_io(
                    self.client.query_points,
                    collection_name=self.collection_name,
                    query=prefetch[0].query,
                    using=prefetch[0].using,
                    query_filter=query_filter,
                    limit=search_limit,
                    with_payload=["chunk_id"],
                )
            else:
                response = await self._index_io(
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

    def _source_filter(self, source_id: uuid.UUID):
        return models.Filter(must=[
            models.FieldCondition(
                key="source_id", match=models.MatchValue(value=str(source_id))
            )
        ])

    def _delete_source_points(self, source_id: uuid.UUID, point_ids: Optional[list[str]] = None) -> None:
        if self.client is None or models is None:
            return
        source_filter = self._source_filter(source_id)
        if point_ids is not None:
            if not point_ids:
                return
            source_filter.must.append(models.HasIdCondition(has_id=point_ids))
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(filter=source_filter),
            wait=True,
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
        readable_sources = getattr(filters, "readable_source_ids", None)
        if readable_sources is not None:
            must.append(models.FieldCondition(
                key="source_id", match=models.MatchAny(any=[str(value) for value in readable_sources]),
            ))
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
            # The Qdrant hash covers the complete deterministic embedding
            # context.  Keep the source chunk hash separately for callers that
            # need to identify a body-only change.
            "content_hash": self._index_content_hash(chunk, document, source),
            "embedding_fingerprint": self._embedding_fingerprint(),
            "chunk_content_hash": chunk.content_hash,
            "tags": document.tags or [],
            "project_refs": [str(ref) for ref in document.project_refs or []],
            "task_refs": [str(ref) for ref in document.task_refs or []],
            "source_created_at": self._serialize_datetime(
                getattr(source, "created_at", None)
            ),
            "source_updated_at": self._serialize_datetime(
                getattr(source, "updated_at", None)
            ),
            "source_last_synced_at": self._serialize_datetime(
                getattr(source, "last_synced_at", None)
            ),
            "document_created_at": self._serialize_datetime(
                getattr(document, "created_at", None)
            ),
            "document_updated_at": self._serialize_datetime(
                getattr(document, "updated_at", None)
            ),
            "document_modified_at": self._serialize_datetime(
                getattr(document, "modified_at", None)
            ),
            "document_date": self._serialize_datetime(
                getattr(document, "document_date", None)
            ),
            "document_date_source": getattr(document, "document_date_source", None),
            "document_last_indexed_at": self._serialize_datetime(
                getattr(document, "last_indexed_at", None)
            ),
            "chunk_created_at": self._serialize_datetime(
                getattr(chunk, "created_at", None)
            ),
        }

    @staticmethod
    def _serialize_datetime(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
            return value.isoformat()
        if isinstance(value, date):
            return datetime.combine(value, time.min).replace(
                tzinfo=timezone.utc
            ).isoformat()
        isoformat = getattr(value, "isoformat", None)
        if callable(isoformat):
            return str(isoformat())
        return str(value)

    @staticmethod
    def _metadata_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return " ".join(str(item) for item in value)
        return str(value)

    def _dense_text(
        self,
        chunk: KnowledgeChunk,
        document: KnowledgeDocument,
        source: KnowledgeSource,
    ) -> str:
        """Build deterministic context for the dense embedding lane.

        The query side still embeds the user's raw query.  Adding stable
        source/document hierarchy to indexed chunks keeps otherwise identical
        bodies distinguishable without an LLM-generated enrichment step.
        """
        heading_path = " > ".join(
            str(item) for item in (chunk.heading_path or [])
        )
        return "\n".join(
            [
                f"Source: {source.name or ''}",
                f"Source type: {source.source_type or ''}",
                f"Document title: {document.title or ''}",
                f"Document path: {document.path or ''}",
                f"Tags: {self._metadata_text(document.tags)}",
                f"Project refs: {self._metadata_text(document.project_refs)}",
                f"Task refs: {self._metadata_text(document.task_refs)}",
                f"Heading: {heading_path}",
                f"Chunk index: {chunk.chunk_index}",
                f"Chunk: {chunk.text or ''}",
            ]
        )

    def _index_content_hash(
        self,
        chunk: KnowledgeChunk,
        document: KnowledgeDocument,
        source: KnowledgeSource,
    ) -> str:
        """Hash the exact dense input so metadata changes trigger reindexing."""
        dense_text = self._dense_text(chunk, document, source)
        return hashlib.sha256(
            dense_text.encode("utf-8", errors="replace")
        ).hexdigest()

    def _embedding_fingerprint(self) -> str:
        identity = {
            "model": self.embedding.config.model,
            "dimension": self.embedding.dimension,
            "input_version": self.dense_input_version,
        }
        return hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

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
