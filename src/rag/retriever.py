"""
RAG retriever with reranking support.
"""

import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from .embedding import BgeM3Embedding
from .qdrant_client import QdrantManager, SearchResult
from .reranker import BgeReranker, RerankResult
from .config import SearchConfig

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """Final retrieval result after reranking."""
    text: str
    score: float
    source_file: str
    metadata: Dict[str, Any]


class Retriever:
    """RAG retriever with embedding search and reranking."""
    
    def __init__(
        self,
        qdrant: QdrantManager,
        embedding: BgeM3Embedding,
        reranker: BgeReranker,
        search_config: SearchConfig
    ):
        """Initialize retriever.
        
        Args:
            qdrant: Qdrant manager
            embedding: Embedding model
            reranker: Reranker model
            search_config: Search configuration
        """
        self.qdrant = qdrant
        self.embedding = embedding
        self.reranker = reranker
        self.search_config = search_config
    
    async def retrieve(
        self,
        query: str,
        top_k: Optional[int] = None,
        top_n: Optional[int] = None,
        filter_conditions: Optional[Dict[str, Any]] = None,
        use_reranking: bool = True
    ) -> List[RetrievalResult]:
        """Retrieve relevant documents for a query.
        
        Args:
            query: Query text
            top_k: Number of initial candidates from vector search
            top_n: Number of final results after reranking
            filter_conditions: Optional metadata filters
            use_reranking: Whether to apply reranking
            
        Returns:
            List of retrieval results
        """
        if top_k is None:
            top_k = self.search_config.top_k
        if top_n is None:
            top_n = self.search_config.top_n
        # Ensure int for slice operations (LLM may pass float like 5.0)
        top_k = int(top_k)
        top_n = int(top_n)
        
        try:
            # Step 1: Generate query embedding
            query_embedding = await self.embedding.embed_query(query)
            if query_embedding is None:
                logger.error("Failed to generate query embedding")
                return []
            
            # Step 2: Vector search
            initial_results = await self.qdrant.search(
                query_embedding=query_embedding,
                top_k=top_k,
                filter_conditions=filter_conditions
            )
            
            if not initial_results:
                logger.info("No results found from vector search")
                return []
            
            logger.info(f"Vector search returned {len(initial_results)} candidates")
            
            # Step 3: Reranking (if enabled and reranker available)
            if use_reranking and self.reranker._initialized:
                documents = [r.text for r in initial_results]
                metadata_list = [
                    {"original_result": r, **r.metadata}
                    for r in initial_results
                ]
                
                reranked = await self.reranker.rerank(
                    query=query,
                    documents=documents,
                    metadata_list=metadata_list,
                    top_n=top_n
                )
                
                if reranked:
                    results = []
                    for rr in reranked:
                        original = rr.metadata.get("original_result")
                        results.append(RetrievalResult(
                            text=rr.text,
                            score=rr.score,
                            source_file=original.metadata.get("source_file", "") if original else "",
                            metadata={k: v for k, v in rr.metadata.items() if k != "original_result"}
                        ))
                    
                    logger.info(f"Reranking returned {len(results)} results")
                    return results
            
            # Fallback: return top_n from vector search
            results = []
            for r in initial_results[:top_n]:
                results.append(RetrievalResult(
                    text=r.text,
                    score=r.score,
                    source_file=r.metadata.get("source_file", ""),
                    metadata=r.metadata
                ))
            
            return results
            
        except Exception as e:
            logger.error(f"Retrieval failed: {e}")
            return []
    
    async def retrieve_simple(
        self,
        query: str,
        top_n: int = 5
    ) -> List[str]:
        """Simple retrieval returning only text content.
        
        Args:
            query: Query text
            top_n: Number of results
            
        Returns:
            List of retrieved texts
        """
        results = await self.retrieve(query, top_n=top_n)
        return [r.text for r in results]
    
    async def retrieve_with_context(
        self,
        query: str,
        top_n: int = 5,
        include_sources: bool = True
    ) -> str:
        """Retrieve and format as context for LLM.
        
        Args:
            query: Query text
            top_n: Number of results
            include_sources: Whether to include source information
            
        Returns:
            Formatted context string
        """
        results = await self.retrieve(query, top_n=top_n)
        
        if not results:
            return ""
        
        context_parts = []
        for i, r in enumerate(results, 1):
            if include_sources and r.source_file:
                source_name = r.source_file.split('/')[-1].split('\\')[-1]
                context_parts.append(f"[{i}] (Source: {source_name})\n{r.text}")
            else:
                context_parts.append(f"[{i}]\n{r.text}")
        
        return "\n\n".join(context_parts)
