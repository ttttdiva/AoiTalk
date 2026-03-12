"""
Reranker for RAG search results using sentence-transformers CrossEncoder.

Uses a cross-encoder model for more accurate relevance scoring.
"""

import logging
import asyncio
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .config import RerankerConfig

logger = logging.getLogger(__name__)

# Lazy imports
_CrossEncoder = None


def _load_cross_encoder():
    """Lazy load sentence-transformers CrossEncoder."""
    global _CrossEncoder
    if _CrossEncoder is None:
        try:
            from sentence_transformers import CrossEncoder
            _CrossEncoder = CrossEncoder
        except ImportError:
            logger.warning("sentence-transformers not installed. Reranking features will be disabled.")
    return _CrossEncoder


@dataclass
class RerankResult:
    """Reranked result."""
    text: str
    score: float
    original_index: int
    metadata: dict


class BgeReranker:
    """Reranker using sentence-transformers CrossEncoder.
    
    CrossEncoder models take query-document pairs and output relevance scores,
    providing more accurate ranking than bi-encoder similarity.
    
    Supported models:
    - cross-encoder/ms-marco-MiniLM-L-6-v2 (fast, English)
    - BAAI/bge-reranker-base (multilingual)
    - cross-encoder/ms-marco-MiniLM-L-12-v2 (better quality)
    """
    
    # Model mapping for config compatibility
    MODEL_MAPPING = {
        "BAAI/bge-reranker-v2-gemma": "cross-encoder/ms-marco-MiniLM-L-6-v2",  # Fallback
        "BAAI/bge-reranker-base": "cross-encoder/ms-marco-MiniLM-L-6-v2",  # Fallback
        "BAAI/bge-reranker-large": "cross-encoder/ms-marco-MiniLM-L-12-v2",  # Fallback
    }
    
    def __init__(self, config: RerankerConfig):
        """Initialize reranker model.
        
        Args:
            config: Reranker configuration
        """
        self.config = config
        self.model = None
        self._initialized = False
        self._executor = ThreadPoolExecutor(max_workers=1)
        
        # Map model name if needed
        self._model_name = self.MODEL_MAPPING.get(
            config.model, 
            config.model
        )
        
        # Check if we need to use a fallback
        if self._model_name != config.model:
            logger.info(f"Using fallback model: {self._model_name} instead of {config.model}")
    
    async def initialize(self) -> bool:
        """Initialize the reranker model.
        
        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True
        
        CrossEncoder = _load_cross_encoder()
        if CrossEncoder is None:
            return False
        
        try:
            logger.info(f"Loading reranker model: {self._model_name}")
            
            loop = asyncio.get_event_loop()
            
            def _load_model():
                return CrossEncoder(
                    self._model_name,
                    device=self.config.device
                )
            
            self.model = await loop.run_in_executor(self._executor, _load_model)
            
            self._initialized = True
            logger.info(f"Reranker model loaded successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load reranker model: {e}")
            return False
    
    async def rerank(
        self,
        query: str,
        documents: List[str],
        metadata_list: Optional[List[dict]] = None,
        top_n: Optional[int] = None
    ) -> List[RerankResult]:
        """Rerank documents by relevance to query.
        
        Args:
            query: Query text
            documents: List of document texts to rerank
            metadata_list: Optional metadata for each document
            top_n: Number of top results to return (default: config.top_n)
            
        Returns:
            List of reranked results sorted by score (descending)
        """
        if not self._initialized or self.model is None:
            logger.error("Reranker model not initialized")
            return []
        
        if not documents:
            return []
        
        if metadata_list is None:
            metadata_list = [{} for _ in documents]
        
        if top_n is None:
            top_n = self.config.top_n
        # Ensure top_n is int for slice operations (LLM may pass float like 5.0)
        top_n = int(top_n)
        
        try:
            loop = asyncio.get_event_loop()
            
            # Create query-document pairs for cross-encoder
            pairs = [(query, doc) for doc in documents]
            
            # Compute scores in executor to avoid blocking
            def _predict():
                return self.model.predict(pairs, show_progress_bar=False)
            
            scores = await loop.run_in_executor(self._executor, _predict)
            
            # Ensure scores is a list
            if hasattr(scores, 'tolist'):
                scores = scores.tolist()
            elif not isinstance(scores, list):
                scores = [float(scores)]
            
            # Create results with original indices
            results = []
            for i, (score, text, metadata) in enumerate(zip(scores, documents, metadata_list)):
                results.append(RerankResult(
                    text=text,
                    score=float(score),
                    original_index=i,
                    metadata=metadata
                ))
            
            # Sort by score descending
            results.sort(key=lambda x: x.score, reverse=True)
            
            logger.debug(f"Reranked {len(documents)} documents, returning top {top_n}")
            
            # Return top_n results
            return results[:top_n]
            
        except Exception as e:
            logger.error(f"Failed to rerank documents: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return []
    
    def close(self):
        """Cleanup resources."""
        self._executor.shutdown(wait=False)
        self.model = None
        self._initialized = False
