"""
RAG Manager - Central manager for RAG operations.
"""

import logging
from typing import Optional, List, Dict, Any
from pathlib import Path
from copy import deepcopy

from .config import RagConfig, get_rag_config, QdrantConfig, get_project_collection_name
from .qdrant_client import QdrantManager
from .embedding import BgeM3Embedding
from .reranker import BgeReranker
from .indexer import DocumentIndexer
from .retriever import Retriever, RetrievalResult

logger = logging.getLogger(__name__)

# Shared models (heavy, should be loaded once)
_shared_embedding: Optional[BgeM3Embedding] = None
_shared_reranker: Optional[BgeReranker] = None


class RagManager:
    """Central manager for RAG operations."""
    
    def __init__(self, config: Optional[RagConfig] = None, project_id: Optional[str] = None,
                 target_collection_name: Optional[str] = None):
        """Initialize RAG manager.

        Args:
            config: RAG configuration (uses global config if None)
            project_id: Optional project ID for project-specific collection
            target_collection_name: Optional direct Qdrant collection name (overrides project_id)
        """
        self.config = config or get_rag_config()
        self.project_id = project_id
        self._target_collection_name = target_collection_name

        # Components (initialized lazily)
        self._qdrant: Optional[QdrantManager] = None
        self._embedding: Optional[BgeM3Embedding] = None
        self._reranker: Optional[BgeReranker] = None
        self._indexer: Optional[DocumentIndexer] = None
        self._retriever: Optional[Retriever] = None

        self._initialized = False
    
    async def initialize(self) -> bool:
        """Initialize all RAG components.
        
        Uses shared embedding/reranker models across all projects.
        Creates project-specific Qdrant collection if project_id is set.
        
        Returns:
            True if initialization successful
        """
        global _shared_embedding, _shared_reranker
        
        if self._initialized:
            return True
        
        if not self.config.enabled:
            logger.info("RAG is disabled in configuration")
            return False
        
        try:
            # Determine collection name
            if self._target_collection_name:
                collection_name = self._target_collection_name
            else:
                collection_name = get_project_collection_name(self.project_id)
            logger.info(f"Initializing RAG for collection: {collection_name}")
            
            # Use shared embedding model (heavy, load once)
            if _shared_embedding is None:
                logger.info("Loading shared embedding model...")
                _shared_embedding = BgeM3Embedding(self.config.embedding)
                if not await _shared_embedding.initialize():
                    logger.error("Failed to initialize embedding model")
                    return False
            self._embedding = _shared_embedding
            
            # Create project-specific Qdrant config with appropriate collection name
            project_qdrant_config = QdrantConfig(
                host=self.config.qdrant.host,
                port=self.config.qdrant.port,
                collection_name=collection_name,
                api_key=self.config.qdrant.api_key,
                local_path=self.config.qdrant.local_path
            )
            
            # Initialize Qdrant with project-specific collection
            self._qdrant = QdrantManager(
                project_qdrant_config,
                vector_size=self._embedding.dimension
            )
            if not await self._qdrant.initialize():
                logger.error(f"Failed to initialize Qdrant for collection: {collection_name}")
                return False
            
            # Use shared reranker model (heavy, load once)
            if _shared_reranker is None:
                logger.info("Loading shared reranker model...")
                _shared_reranker = BgeReranker(self.config.reranker)
                if not await _shared_reranker.initialize():
                    logger.warning("Reranker initialization failed, will use vector search only")
            self._reranker = _shared_reranker
            
            # Initialize indexer
            self._indexer = DocumentIndexer(
                qdrant=self._qdrant,
                embedding=self._embedding,
                chunking_config=self.config.chunking,
                source_config=self.config.source,
                indexing_config=self.config.indexing
            )
            await self._indexer.initialize()
            
            # Initialize retriever
            self._retriever = Retriever(
                qdrant=self._qdrant,
                embedding=self._embedding,
                reranker=self._reranker,
                search_config=self.config.search
            )
            
            self._initialized = True
            logger.info(f"RAG initialization complete for collection: {collection_name}")
            return True
            
        except Exception as e:
            logger.error(f"RAG initialization failed: {e}")
            return False
    
    async def search(
        self,
        query: str,
        top_n: Optional[int] = None,
        use_reranking: bool = True
    ) -> List[RetrievalResult]:
        """Search for relevant documents.
        
        Args:
            query: Search query
            top_n: Number of results to return
            use_reranking: Whether to use reranking
            
        Returns:
            List of retrieval results
        """
        if not self._initialized:
            if not await self.initialize():
                return []
        
        return await self._retriever.retrieve(
            query=query,
            top_n=top_n,
            use_reranking=use_reranking
        )
    
    async def search_simple(self, query: str, top_n: int = 5) -> List[str]:
        """Simple search returning only text content.
        
        Args:
            query: Search query
            top_n: Number of results
            
        Returns:
            List of text contents
        """
        if not self._initialized:
            if not await self.initialize():
                return []
        
        return await self._retriever.retrieve_simple(query, top_n)
    
    async def get_context(
        self,
        query: str,
        top_n: int = 5,
        include_sources: bool = True
    ) -> str:
        """Get formatted context for LLM.
        
        Args:
            query: Search query
            top_n: Number of results
            include_sources: Include source information
            
        Returns:
            Formatted context string
        """
        if not self._initialized:
            if not await self.initialize():
                return ""
        
        return await self._retriever.retrieve_with_context(
            query=query,
            top_n=top_n,
            include_sources=include_sources
        )
    
    async def index_file(self, file_path: str) -> int:
        """Index a single file.
        
        Args:
            file_path: Path to file
            
        Returns:
            Number of chunks indexed
        """
        if not self._initialized:
            if not await self.initialize():
                return 0
        
        return await self._indexer.index_file(file_path)
    
    async def index_directory(
        self,
        directory: str,
        recursive: bool = True
    ) -> Dict[str, int]:
        """Index all matching files in a directory.
        
        Args:
            directory: Directory path
            recursive: Whether to search recursively
            
        Returns:
            Dict mapping file paths to chunk counts
        """
        if not self._initialized:
            if not await self.initialize():
                return {}
        
        return await self._indexer.index_directory(directory, recursive)
    
    async def index_text(
        self,
        text: str,
        source_name: str = "manual_input",
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """Index raw text content.
        
        Args:
            text: Text to index
            source_name: Source identifier
            metadata: Additional metadata
            
        Returns:
            Number of chunks indexed
        """
        if not self._initialized:
            if not await self.initialize():
                return 0
        
        return await self._indexer.index_text(text, source_name, metadata)
    
    async def get_collection_info(self) -> Optional[Dict[str, Any]]:
        """Get information about the Qdrant collection.
        
        Returns:
            Collection info or None
        """
        if not self._initialized or self._qdrant is None:
            return None
        
        return await self._qdrant.get_collection_info()
    
    async def delete_file_index(self, source_file: str) -> bool:
        """Delete all indexed chunks for a specific file.

        Args:
            source_file: Source file path to remove from index

        Returns:
            True if successful
        """
        if not self._initialized or self._qdrant is None:
            return False

        return await self._qdrant.delete_by_source_file(source_file)

    async def search_across_collections(
        self,
        collection_names: List[str],
        query: str,
        top_n: int = 5,
        use_reranking: bool = True
    ) -> List[RetrievalResult]:
        """Search across multiple Qdrant collections and merge results.

        Args:
            collection_names: List of Qdrant collection names to search
            query: Search query
            top_n: Number of final results
            use_reranking: Whether to rerank merged results

        Returns:
            List of retrieval results sorted by relevance
        """
        if not self._initialized:
            if not await self.initialize():
                return []

        if not collection_names:
            return []

        # Single collection: use normal search
        if len(collection_names) == 1:
            # Create a temporary manager for that collection
            mgr = get_rag_manager_for_collection(collection_names[0])
            if not await mgr.initialize():
                return []
            return await mgr.search(query, top_n=top_n, use_reranking=use_reranking)

        # Multiple collections: gather results from each
        all_results: List[RetrievalResult] = []
        per_collection_k = max(self.config.search.top_k // len(collection_names), 5)

        for col_name in collection_names:
            try:
                mgr = get_rag_manager_for_collection(col_name)
                if not await mgr.initialize():
                    continue
                results = await mgr.search(
                    query, top_n=per_collection_k, use_reranking=False
                )
                for r in results:
                    r.metadata["_source_collection"] = col_name
                all_results.extend(results)
            except Exception as e:
                logger.warning(f"Search failed for collection {col_name}: {e}")

        if not all_results:
            return []

        # Rerank across all results
        if use_reranking and self._reranker and self._reranker._initialized:
            texts = [r.text for r in all_results]
            reranked = await self._reranker.rerank(
                query=query, documents=texts, top_n=top_n
            )
            final = []
            for item in reranked[:top_n]:
                idx = item.original_index
                if idx < len(all_results):
                    orig = all_results[idx]
                    final.append(RetrievalResult(
                        text=orig.text,
                        score=item.score,
                        metadata=orig.metadata,
                        source_file=orig.source_file
                    ))
            return final
        else:
            # Sort by score
            all_results.sort(key=lambda r: r.score, reverse=True)
            return all_results[:top_n]

    async def clear_index(self) -> bool:
        """Clear all indexed documents.
        
        Returns:
            True if successful
        """
        if not self._initialized or self._qdrant is None:
            return False
        
        return await self._qdrant.recreate_collection()
    
    def close(self):
        """Cleanup resources.
        
        Note: Does not close shared embedding/reranker models.
        """
        if self._qdrant:
            self._qdrant.close()
        # Don't close shared embedding/reranker - they're managed globally
        
        self._initialized = False
        collection_name = get_project_collection_name(self.project_id)
        logger.info(f"RAG manager closed for collection: {collection_name}")


# Global instances
_rag_manager: Optional[RagManager] = None
_project_managers: Dict[str, RagManager] = {}


def get_rag_manager(config: Optional[RagConfig] = None, project_id: Optional[str] = None) -> RagManager:
    """Get or create RAG manager instance.
    
    Args:
        config: RAG configuration (only used on first call)
        project_id: Optional project ID for project-specific collection
        
    Returns:
        RagManager instance
    """
    global _rag_manager, _project_managers
    
    if project_id:
        # Return project-specific manager
        if project_id not in _project_managers:
            _project_managers[project_id] = RagManager(config, project_id)
            logger.info(f"Created RAG manager for project: {project_id}")
        return _project_managers[project_id]
    
    # Return default manager
    if _rag_manager is None:
        _rag_manager = RagManager(config)
    
    return _rag_manager


_collection_managers: Dict[str, RagManager] = {}


def get_rag_manager_for_collection(collection_name: str) -> RagManager:
    """Get or create RAG manager for a specific Qdrant collection name.

    Args:
        collection_name: Qdrant collection name (e.g. 'col_abc123_documents')

    Returns:
        RagManager instance configured for the specified collection
    """
    global _collection_managers

    if collection_name not in _collection_managers:
        _collection_managers[collection_name] = RagManager(
            target_collection_name=collection_name
        )
        logger.info(f"Created RAG manager for collection: {collection_name}")
    return _collection_managers[collection_name]


def get_project_rag_manager(project_id: str) -> RagManager:
    """Get RAG manager for a specific project.
    
    Convenience function for getting project-specific manager.
    
    Args:
        project_id: Project UUID string
        
    Returns:
        RagManager instance for the project
    """
    return get_rag_manager(project_id=project_id)


def close_shared_models():
    """Close shared embedding and reranker models.
    
    Should only be called during application shutdown.
    """
    global _shared_embedding, _shared_reranker
    
    if _shared_embedding:
        _shared_embedding.close()
        _shared_embedding = None
    if _shared_reranker:
        _shared_reranker.close()
        _shared_reranker = None
    logger.info("Shared RAG models closed")

