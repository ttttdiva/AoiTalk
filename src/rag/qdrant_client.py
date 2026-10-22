"""
Qdrant vector database client for RAG.

Supports two modes:
1. Server mode: Connect to a running Qdrant server (Docker or standalone)
2. Local mode: Use local file storage (no server required, like SQLite)
"""

import contextlib
import logging
import threading
import math
import re
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models
    from qdrant_client.http.models import Distance, VectorParams, PointStruct
    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False

from .config import QdrantConfig
from .embedding_identity import DIMENSION, EMBEDDING_FINGERPRINT, embedding_collection_name
from .index_io import run_index_io
from ..security.field_crypto import (
    FieldCryptoError,
    decrypt_text_if_needed,
    encrypt_text,
)

logger = logging.getLogger(__name__)


class QdrantSearchUnavailable(RuntimeError):
    """Sanitized opt-in failure distinguishing outage from zero hits."""

    def __init__(self, code: str = "backend_failed"):
        self.code = code if code in {"index_unavailable", "backend_failed"} else "backend_failed"
        super().__init__(self.code)


_CORRUPT_PAYLOAD_FIELD_ERRORS = {
    "unsupported encrypted field format",
    "encrypted field authentication failed",
}


def _is_corrupt_payload_field_error(error: FieldCryptoError) -> bool:
    """Classify per-point ciphertext damage without exposing payload details."""

    return str(error) in _CORRUPT_PAYLOAD_FIELD_ERRORS


# ローカル(embedded)モードは local_path ごとに単一の QdrantLocal クライアントを
# 共有する(SharedQdrantClient)。QdrantLocal はスレッド安全でないため、
# to_thread 経由の並行クエリを直列化する。server モードでは不要。
# Every embedded collection shares the same client. Use a thread lock across
# writers, readers, and sync-bridge event loops; an asyncio Lock is insufficient.
_LOCAL_INDEX_IO_LOCK = threading.RLock()
_OWNED_EMBEDDING_COLLECTION = re.compile(
    r"^aoitalk_(?:docs|knowledge|media|conversation_memory)__eg2_[0-9a-f]{16}$"
)


def _uses_drvfs_managed_storage(config: QdrantConfig) -> bool:
    """Limit the optimizer override to AoiTalk's Windows WSL drive bind."""
    return bool(
        config.mode == "local-managed"
        and not config.local_path
        and config.wsl_distribution
        and isinstance(config.storage_path, str)
        and re.match(r"^/mnt/[A-Za-z](?:/|$)", config.storage_path)
        and _OWNED_EMBEDDING_COLLECTION.fullmatch(config.collection_name)
    )


def _drvfs_optimizer_config(config: QdrantConfig):
    """Return typed optimizer config for a future owned WSL collection."""
    if not QDRANT_AVAILABLE or not _uses_drvfs_managed_storage(config):
        return None
    return models.OptimizersConfigDiff(max_optimization_threads=0)


class SharedQdrantClient:
    """Singleton wrapper for local-mode QdrantClient.

    Local mode uses file locking, so only one client instance can exist
    per local_path. This class ensures all QdrantManagers share the same
    underlying client when using local storage.
    """
    _instance: Optional['QdrantClient'] = None
    _local_path: Optional[str] = None
    _creation_lock = threading.RLock()

    @classmethod
    def get_client(cls, local_path: str) -> 'QdrantClient':
        # Async callers can open storage in executor threads. Serialize with
        # legacy synchronous callers so only one client acquires its file lock.
        with _LOCAL_INDEX_IO_LOCK, cls._creation_lock:
            return cls._get_client_locked(local_path)

    @classmethod
    def _get_client_locked(cls, local_path: str) -> 'QdrantClient':
        if not QDRANT_AVAILABLE:
            raise RuntimeError("qdrant-client not installed")
        resolved = str(Path(local_path).resolve())
        if cls._instance is None or cls._local_path != resolved:
            if cls._instance is not None:
                try:
                    cls._instance.close()
                except Exception:
                    pass
            Path(resolved).mkdir(parents=True, exist_ok=True)
            cls._instance = QdrantClient(path=resolved)
            cls._local_path = resolved
            logger.info(f"SharedQdrantClient: opened local storage at {resolved}")
        return cls._instance

    @classmethod
    def close(cls):
        with _LOCAL_INDEX_IO_LOCK, cls._creation_lock:
            cls._close_locked()

    @classmethod
    def _close_locked(cls):
        if cls._instance is not None:
            try:
                cls._instance.close()
            except Exception:
                pass
            cls._instance = None
            cls._local_path = None

    @classmethod
    def list_all_collections(cls, config: QdrantConfig) -> List[Dict[str, Any]]:
        """List all Qdrant collections with basic info."""
        guard = _LOCAL_INDEX_IO_LOCK if config.local_path else contextlib.nullcontext()
        with guard:
            return cls._list_all_collections(config)

    @classmethod
    def _list_all_collections(cls, config: QdrantConfig) -> List[Dict[str, Any]]:
        if not QDRANT_AVAILABLE:
            return []
        try:
            if config.local_path:
                client = cls.get_client(config.local_path)
            else:
                client = QdrantClient(
                    host=config.host, port=config.port, api_key=config.api_key
                )
            collections = client.get_collections()
            result = []
            for c in collections.collections:
                try:
                    info = client.get_collection(c.name)
                    result.append({
                        "name": c.name,
                        "points_count": info.points_count,
                        "vectors_count": getattr(info, "vectors_count", None),
                        "status": info.status.value if info.status else "unknown",
                    })
                except Exception:
                    result.append({"name": c.name, "points_count": 0, "status": "unknown"})
            if not config.local_path:
                client.close()
            return result
        except Exception as e:
            logger.error(f"Failed to list collections: {e}")
            return []


@dataclass
class SearchResult:
    """Search result from Qdrant."""
    id: str
    score: float
    text: str
    metadata: Dict[str, Any]


class _LocalMatchValues(list):
    """Keep Qdrant's JSON list while making local membership constant time."""

    def __init__(self, values):
        super().__init__(values)
        self._members = frozenset(self)

    def __contains__(self, value):
        return value in self._members


class QdrantManager:
    """Manager for Qdrant vector database operations.
    
    Supports two modes:
    - Server mode: Connect to Qdrant server via host:port
    - Local mode: Use local file storage (set local_path in config)
    """
    
    def __init__(self, config: QdrantConfig, vector_size: int = DIMENSION):
        """Initialize Qdrant manager.
        
        Args:
            config: Qdrant configuration
            vector_size: Dimension of the pinned embedding vectors (768)
        """
        from dataclasses import replace
        self.config = replace(config, collection_name=embedding_collection_name(config.collection_name))
        if vector_size != DIMENSION:
            raise ValueError("EmbeddingGemma 2 requires one 768-dimensional vector space")
        self.vector_size = DIMENSION
        self.client: Optional['QdrantClient'] = None
        self._initialized = False
        self._is_local_mode = False
        self._point_id_counter = 0
        
        if not QDRANT_AVAILABLE:
            logger.warning("qdrant-client not installed. RAG features will be disabled.")
    
    async def _io(self, operation, *args, **kwargs):
        return await run_index_io(
            operation, *args,
            lock=_LOCAL_INDEX_IO_LOCK if self.config.local_path else None,
            **kwargs,
        )

    async def initialize(self) -> bool:
        return await self._io(self._initialize)

    def _initialize(self) -> bool:
        """Initialize connection to Qdrant.
        
        Uses local file storage mode if config.local_path is set,
        otherwise connects to Qdrant server.
        
        Returns:
            True if connection successful
        """
        if not QDRANT_AVAILABLE:
            return False
            
        if self._initialized:
            return True
        
        try:
            # Determine mode
            if self.config.local_path:
                # Local file storage mode (no Docker required)
                # Use SharedQdrantClient to avoid file lock conflicts
                logger.info(f"Using Qdrant local storage: {self.config.local_path}")
                self.client = SharedQdrantClient.get_client(self.config.local_path)
                self._is_local_mode = True
            else:
                # Server mode
                logger.info(f"Connecting to Qdrant server: {self.config.host}:{self.config.port}")
                self.client = QdrantClient(
                    host=self.config.host,
                    port=self.config.port,
                    api_key=self.config.api_key
                )
                self._is_local_mode = False
            
            # Check if collection exists
            collections = self.client.get_collections()
            collection_names = [c.name for c in collections.collections]
            
            if self.config.collection_name not in collection_names:
                logger.info(f"Creating collection: {self.config.collection_name}")
                create_kwargs = {
                    "collection_name": self.config.collection_name,
                    "vectors_config": VectorParams(
                        size=self.vector_size,
                        distance=Distance.COSINE
                    )
                }
                optimizer_config = _drvfs_optimizer_config(self.config)
                if optimizer_config is not None:
                    create_kwargs["optimizers_config"] = optimizer_config
                self.client.create_collection(**create_kwargs)
            else:
                # Get current point count for ID generation
                info = self.client.get_collection(self.config.collection_name)
                vectors = info.config.params.vectors
                if isinstance(vectors, dict) or vectors.size != self.vector_size:
                    raise RuntimeError("Incompatible embedding collection; explicit reindex required")
                self._point_id_counter = info.points_count or 0
            
            # Local Qdrant ignores payload indexes and emits noisy warnings.
            if not self._is_local_mode:
                self.client.create_payload_index(
                    collection_name=self.config.collection_name,
                    field_name="doc_id",
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
                self.client.create_payload_index(
                    collection_name=self.config.collection_name,
                    field_name="source_file",
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
            
            self._initialized = True
            mode_str = "local storage" if self._is_local_mode else f"server ({self.config.host}:{self.config.port})"
            logger.info(f"Qdrant initialized: {mode_str}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize Qdrant: {e}")
            return False
    
    async def has_verified_document(self, doc_id: str, input_hash: str) -> bool:
        """Verify persisted vectors before a migration skips an unchanged input."""
        if not self._initialized or self.client is None:
            return False
        points = await self._io(self.client.retrieve,
            collection_name=self.config.collection_name,
            ids=[str(uuid.uuid5(uuid.NAMESPACE_URL, "aoitalk:" + doc_id))],
            with_payload=True, with_vectors=True)
        if len(points) != 1:
            return False
        point = points[0]
        payload = point.payload or {}
        vector = point.vector
        return (payload.get("doc_id") == doc_id
                and payload.get("embedding_fingerprint") == EMBEDDING_FINGERPRINT
                and payload.get("embedding_input_hash") == input_hash
                and isinstance(vector, list) and len(vector) == self.vector_size
                and all(math.isfinite(float(x)) for x in vector)
                and abs(sum(float(x)**2 for x in vector) - 1.0) <= .01)

    async def add_documents(
        self,
        ids: List[str],
        embeddings: List[List[float]],
        texts: List[str],
        metadata_list: List[Dict[str, Any]]
    ) -> bool:
        """Add documents to the collection.
        
        Args:
            ids: Document IDs
            embeddings: Embedding vectors
            texts: Original text content
            metadata_list: Metadata for each document
            
        Returns:
            True if successful
        """
        if not self._initialized or self.client is None:
            logger.error("Qdrant not initialized")
            return False
        
        try:
            if not (len(ids) == len(embeddings) == len(texts) == len(metadata_list)):
                raise ValueError("Embedding/document counts differ")
            if any(len(v) != self.vector_size or not all(math.isfinite(float(x)) for x in v)
                   or abs(sum(float(x)**2 for x in v)-1.0) > .01 for v in embeddings):
                raise ValueError("Invalid dense embedding dimension or values")
            points = []
            for doc_id, embedding, text, metadata in zip(ids, embeddings, texts, metadata_list):
                payload = {
                    "text": encrypt_text(text, aad=f"qdrant.payload.text:{doc_id}"),
                    "doc_id": doc_id,
                    **metadata,
                    "embedding_fingerprint": EMBEDDING_FINGERPRINT,
                }
                # Stable IDs make resumed migration an idempotent upsert.
                points.append(PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, "aoitalk:" + doc_id)),
                    vector=embedding,
                    payload=payload
                ))
                self._point_id_counter += 1
            
            await self._io(self.client.upsert,
                collection_name=self.config.collection_name,
                points=points
            )
            
            logger.info(f"Added {len(points)} documents to Qdrant")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add documents: {e}")
            return False
    
    async def search(
        self,
        query_embedding: List[float],
        top_k: int = 20,
        filter_conditions: Optional[Dict[str, Any]] = None,
        *,
        raise_on_error: bool = False,
    ) -> List[SearchResult]:
        """Search for similar documents.
        
        Args:
            query_embedding: Query embedding vector
            top_k: Number of results to return
            filter_conditions: Optional filter conditions
            
        Returns:
            List of search results
        """
        if not self._initialized or self.client is None:
            if raise_on_error:
                raise QdrantSearchUnavailable("index_unavailable")
            logger.error("Qdrant not initialized")
            return []
        
        try:
            # Build filter if provided
            query_filter = None
            must_conditions = [models.FieldCondition(key="embedding_fingerprint", match=models.MatchValue(value=EMBEDDING_FINGERPRINT))]
            if filter_conditions:
                for key, value in filter_conditions.items():
                    match = (models.MatchAny(any=value) if isinstance(value, list)
                             else models.MatchValue(value=value))
                    if isinstance(match, models.MatchAny) and self._is_local_mode:
                        # Apply after Pydantic validation, which creates a list.
                        match.any = _LocalMatchValues(match.any)
                    must_conditions.append(
                        models.FieldCondition(
                            key=key,
                            match=match
                        )
                    )
            query_filter = models.Filter(must=must_conditions)
            
            # Sync bridges use separate event loops. The common thread lock
            # serializes reads with writes without binding callers to one loop.
            search_results = []
            invalid_points = 0
            scanned = 0
            # Qdrant's offset preserves the same vector ranking while allowing
            # malformed leading points to be replaced.  Ordinary requests up
            # to 400 may scan at most 4x their requested page, capped at 400;
            # a larger explicit request keeps its own requested size as the
            # scan bound and never triggers an unbounded full-corpus walk.
            max_scan = max(top_k, min(top_k * 4, 400))
            while scanned < max_scan and len(search_results) < top_k:
                page_limit = min(top_k, max_scan - scanned)
                query_options = {
                    "collection_name": self.config.collection_name,
                    "query": query_embedding,
                    "limit": page_limit,
                    "query_filter": query_filter,
                    "with_payload": True,
                }
                if scanned:
                    query_options["offset"] = scanned
                response = await self._io(self.client.query_points, **query_options)
                raw_points = response.points
                if not raw_points:
                    break
                scanned += len(raw_points)
                for hit in raw_points:
                    try:
                        payload = hit.payload
                        if not isinstance(payload, dict):
                            raise ValueError("malformed qdrant payload")

                        raw_doc_id = payload.get("doc_id", hit.id)
                        if raw_doc_id is None:
                            raise ValueError("missing qdrant document id")
                        doc_id = str(raw_doc_id)
                        if not doc_id:
                            raise ValueError("empty qdrant document id")

                        score = float(hit.score)
                        if not math.isfinite(score):
                            raise ValueError("non-finite qdrant score")

                        raw_text = payload.get("text", "")
                        if raw_text is None:
                            raw_text = ""
                        if not isinstance(raw_text, str):
                            raise ValueError("malformed qdrant text payload")
                        text = decrypt_text_if_needed(
                            raw_text,
                            aad=f"qdrant.payload.text:{doc_id}",
                        )
                        if text is None:
                            text = ""
                        if not isinstance(text, str):
                            raise ValueError("malformed qdrant text payload")

                        search_results.append(SearchResult(
                            id=doc_id,
                            score=score,
                            text=text,
                            metadata={
                                key: value
                                for key, value in payload.items()
                                if key not in ["text", "doc_id"]
                            },
                        ))
                    except FieldCryptoError as exc:
                        if not _is_corrupt_payload_field_error(exc):
                            raise
                        invalid_points += 1
                        # A single damaged derivative must not hide healthy
                        # canonical candidates. Keep this log payload-free.
                        logger.warning("Qdrant search discarded an invalid point")
                    except (TypeError, ValueError, UnicodeDecodeError):
                        invalid_points += 1
                        # Point payloads are untrusted index data. Exclude only
                        # the malformed point and keep the ranked page going.
                        logger.warning("Qdrant search discarded an invalid point")
                if len(raw_points) < page_limit:
                    break

            # A non-empty page with no usable point is an index corruption
            # failure in strict mode. Empty Qdrant results remain a successful
            # zero-hit search.
            if scanned and invalid_points and not search_results:
                raise FieldCryptoError("qdrant search returned no valid points")

            return search_results
            
        except Exception as exc:
            logger.warning("Qdrant search backend unavailable")
            if raise_on_error:
                raise QdrantSearchUnavailable("backend_failed") from exc
            return []
    
    async def delete_collection(self) -> bool:
        """Delete the entire collection.
        
        Returns:
            True if successful
        """
        if not self._initialized or self.client is None:
            return False
        
        try:
            await self._io(self.client.delete_collection, self.config.collection_name)
            self._point_id_counter = 0
            logger.info(f"Deleted collection: {self.config.collection_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete collection: {e}")
            return False

    async def recreate_collection(self) -> bool:
        """Delete and recreate the collection.
        
        Returns:
            True if successful
        """
        if not self._initialized or self.client is None:
            return False
            
        try:
            def recreate():
                self.client.recreate_collection(
                    collection_name=self.config.collection_name,
                    vectors_config=models.VectorParams(
                        size=self.vector_size, distance=models.Distance.COSINE
                    ),
                )
                # Retain collection ownership until its schema is complete.
                if not self._is_local_mode:
                    for field in ("doc_id", "source_file"):
                        self.client.create_payload_index(
                            collection_name=self.config.collection_name,
                            field_name=field,
                            field_schema=models.PayloadSchemaType.KEYWORD,
                        )
            await self._io(recreate)
            
            self._point_id_counter = 0
            logger.info(f"Recreated collection: {self.config.collection_name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to recreate collection: {e}")
            return False
    
    async def get_collection_info(self) -> Optional[Dict[str, Any]]:
        """Get collection information.
        
        Returns:
            Collection info or None if not available
        """
        if not self._initialized or self.client is None:
            return None
        
        try:
            info = await self._io(self.client.get_collection, self.config.collection_name)
            return {
                "name": self.config.collection_name,
                "points_count": info.points_count,
                "vectors_count": getattr(info, "vectors_count", None),
                "status": info.status.value,
                "mode": "local" if self._is_local_mode else "server"
            }
        except Exception as e:
            logger.error(f"Failed to get collection info: {e}")
            return None

    async def document_exists(self, doc_id: str) -> bool:
        """Check if a document exists locally or on server.
        
        Args:
            doc_id: The document ID to check
            
        Returns:
            True if document exists
        """
        if not self._initialized or self.client is None:
            return False
            
        try:
            # Create filter for doc_id in payload
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="doc_id",
                        match=models.MatchValue(value=doc_id)
                    )
                ]
            )
            
            # Use count/scroll to check existence
            # For Qdrant, count is efficient
            count_result = await self._io(self.client.count,
                collection_name=self.config.collection_name,
                count_filter=query_filter
            )
            
            return count_result.count > 0
            
        except Exception as e:
            logger.error(f"Failed to check document existence: {e}")
            return False

    async def scan_existing_doc_ids(self) -> set[str]:
        """Retrieve ALL document IDs from the collection.
        
        Optimized for local mode where filters are slow.
        Fetches all points and extracts doc_id payload in batches.
        
        Returns:
            Set of existing doc_ids
        """
        if not self._initialized or self.client is None:
            return set()
            
        try:
            # Check if collection exists first
            try:
                info = await self._io(self.client.get_collection, self.config.collection_name)
                if not info or info.points_count == 0:
                    return set()
            except Exception:
                return set()

            logger.info("Scanning existing documents (this may take a moment)...")
            existing_ids = set()
            
            # Using scroll to fetch all points
            # We only need the payload 'doc_id', no vectors
            next_offset = None
            total_scanned = 0
            
            while True:
                records, next_offset = await self._io(self.client.scroll,
                    collection_name=self.config.collection_name,
                    scroll_filter=None,
                    limit=2000, # Large batch for speed
                    with_payload=["doc_id"],
                    with_vectors=False,
                    offset=next_offset
                )
                
                for record in records:
                    if record.payload and "doc_id" in record.payload:
                        existing_ids.add(record.payload["doc_id"])
                        
                total_scanned += len(records)
                if total_scanned % 10000 == 0:
                    logger.info(f"Scanned {total_scanned} documents...")
                    
                if next_offset is None:
                    break
                    
            logger.info(f"Scan complete. Found {len(existing_ids)} unique documents.")
            return existing_ids
            
        except Exception as e:
            logger.error(f"Failed to scan existing documents: {e}")
            return set()

    async def delete_by_source_file(self, source_file: str) -> bool:
        """Delete all points associated with a source file.

        Args:
            source_file: The source file path to delete chunks for

        Returns:
            True if successful
        """
        if not self._initialized or self.client is None:
            return False

        try:
            await self._io(self.client.delete,
                collection_name=self.config.collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="source_file",
                                match=models.MatchValue(value=source_file)
                            )
                        ]
                    )
                )
            )
            logger.info(f"Deleted chunks for source file: {source_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete by source file: {e}")
            return False

    async def scan_source_file_hashes(self) -> Dict[str, Optional[str]]:
        """Retrieve source_file -> content_hash mapping from all points.

        Scans all points and extracts unique source_file with their content_hash.
        Legacy data without content_hash will have None as value.

        Returns:
            Dict mapping source_file to content_hash (or None)
        """
        if not self._initialized or self.client is None:
            return {}

        try:
            try:
                info = await self._io(self.client.get_collection, self.config.collection_name)
                if not info or info.points_count == 0:
                    return {}
            except Exception:
                return {}

            logger.info("Scanning source file hashes...")
            file_hashes: Dict[str, Optional[str]] = {}
            next_offset = None
            total_scanned = 0

            while True:
                records, next_offset = await self._io(self.client.scroll,
                    collection_name=self.config.collection_name,
                    scroll_filter=None,
                    limit=2000,
                    with_payload=["source_file", "content_hash"],
                    with_vectors=False,
                    offset=next_offset
                )

                for record in records:
                    if record.payload and "source_file" in record.payload:
                        sf = record.payload["source_file"]
                        if sf not in file_hashes:
                            file_hashes[sf] = record.payload.get("content_hash")

                total_scanned += len(records)
                if next_offset is None:
                    break

            logger.info(f"Hash scan complete. Found {len(file_hashes)} indexed files.")
            return file_hashes

        except Exception as e:
            logger.error(f"Failed to scan source file hashes: {e}")
            return {}

    def close(self):
        """Close the Qdrant client connection.

        In local mode the client is shared via SharedQdrantClient,
        so we only mark ourselves as uninitialised without closing it.
        """
        if self._is_local_mode:
            # Don't close shared client – other managers may still use it
            self._initialized = False
        elif self.client is not None:
            self.client.close()
            self._initialized = False
            logger.info("Qdrant connection closed")
