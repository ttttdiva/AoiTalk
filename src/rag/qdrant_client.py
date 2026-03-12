"""
Qdrant vector database client for RAG.

Supports two modes:
1. Server mode: Connect to a running Qdrant server (Docker or standalone)
2. Local mode: Use local file storage (no server required, like SQLite)
"""

import logging
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

logger = logging.getLogger(__name__)


class SharedQdrantClient:
    """Singleton wrapper for local-mode QdrantClient.

    Local mode uses file locking, so only one client instance can exist
    per local_path. This class ensures all QdrantManagers share the same
    underlying client when using local storage.
    """
    _instance: Optional['QdrantClient'] = None
    _local_path: Optional[str] = None

    @classmethod
    def get_client(cls, local_path: str) -> 'QdrantClient':
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
                        "vectors_count": info.vectors_count,
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


class QdrantManager:
    """Manager for Qdrant vector database operations.
    
    Supports two modes:
    - Server mode: Connect to Qdrant server via host:port
    - Local mode: Use local file storage (set local_path in config)
    """
    
    def __init__(self, config: QdrantConfig, vector_size: int = 1024):
        """Initialize Qdrant manager.
        
        Args:
            config: Qdrant configuration
            vector_size: Dimension of embedding vectors (1024 for bge-m3)
        """
        self.config = config
        self.vector_size = vector_size
        self.client: Optional['QdrantClient'] = None
        self._initialized = False
        self._is_local_mode = False
        self._point_id_counter = 0
        
        if not QDRANT_AVAILABLE:
            logger.warning("qdrant-client not installed. RAG features will be disabled.")
    
    async def initialize(self) -> bool:
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
                self.client.create_collection(
                    collection_name=self.config.collection_name,
                    vectors_config=VectorParams(
                        size=self.vector_size,
                        distance=Distance.COSINE
                    )
                )
            else:
                # Get current point count for ID generation
                info = self.client.get_collection(self.config.collection_name)
                self._point_id_counter = info.points_count or 0
            
            # Ensure payload indexes exist for fast filtering
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
            points = []
            for doc_id, embedding, text, metadata in zip(ids, embeddings, texts, metadata_list):
                payload = {
                    "text": text,
                    "doc_id": doc_id,
                    **metadata
                }
                # Use incrementing numeric IDs
                points.append(PointStruct(
                    id=self._point_id_counter,
                    vector=embedding,
                    payload=payload
                ))
                self._point_id_counter += 1
            
            self.client.upsert(
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
        filter_conditions: Optional[Dict[str, Any]] = None
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
            logger.error("Qdrant not initialized")
            return []
        
        try:
            # Build filter if provided
            query_filter = None
            if filter_conditions:
                must_conditions = []
                for key, value in filter_conditions.items():
                    must_conditions.append(
                        models.FieldCondition(
                            key=key,
                            match=models.MatchValue(value=value)
                        )
                    )
                query_filter = models.Filter(must=must_conditions)
            
            results = self.client.search(
                collection_name=self.config.collection_name,
                query_vector=query_embedding,
                limit=top_k,
                query_filter=query_filter
            )
            
            search_results = []
            for hit in results:
                search_results.append(SearchResult(
                    id=hit.payload.get("doc_id", str(hit.id)),
                    score=hit.score,
                    text=hit.payload.get("text", ""),
                    metadata={k: v for k, v in hit.payload.items() 
                             if k not in ["text", "doc_id"]}
                ))
            
            return search_results
            
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []
    
    async def delete_collection(self) -> bool:
        """Delete the entire collection.
        
        Returns:
            True if successful
        """
        if not self._initialized or self.client is None:
            return False
        
        try:
            self.client.delete_collection(self.config.collection_name)
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
            self.client.recreate_collection(
                collection_name=self.config.collection_name,
                vectors_config=models.VectorParams(
                    size=self.vector_size,
                    distance=models.Distance.COSINE
                )
            )
            
            # Re-create payload indexes
            try:
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
            except Exception as e:
                # Log but don't fail if index creation fails (e.g. local mode warning)
                logger.warning(f"Could not create payload index during recreation: {e}")
            
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
            info = self.client.get_collection(self.config.collection_name)
            return {
                "name": self.config.collection_name,
                "points_count": info.points_count,
                "vectors_count": info.vectors_count,
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
            count_result = self.client.count(
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
                info = self.client.get_collection(self.config.collection_name)
                if not info or info.points_count == 0:
                    return set()
            except:
                return set()

            logger.info("Scanning existing documents (this may take a moment)...")
            existing_ids = set()
            
            # Using scroll to fetch all points
            # We only need the payload 'doc_id', no vectors
            next_offset = None
            total_scanned = 0
            
            while True:
                records, next_offset = self.client.scroll(
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
            self.client.delete(
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
                info = self.client.get_collection(self.config.collection_name)
                if not info or info.points_count == 0:
                    return {}
            except Exception:
                return {}

            logger.info("Scanning source file hashes...")
            file_hashes: Dict[str, Optional[str]] = {}
            next_offset = None
            total_scanned = 0

            while True:
                records, next_offset = self.client.scroll(
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
