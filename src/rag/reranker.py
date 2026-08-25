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


def _is_cuda_available() -> bool:
    """Return whether the active PyTorch runtime can use CUDA.

    PyTorch is imported lazily because the reranker is an optional RAG
    dependency.  Missing or broken PyTorch installations are treated as CUDA
    unavailable; ``BgeReranker`` then applies the configured fallback policy
    before constructing a model.
    """
    try:
        import torch
    except Exception:
        logger.debug("Failed to import PyTorch while checking CUDA", exc_info=True)
        return False

    try:
        return bool(torch.cuda.is_available())
    except Exception:
        logger.debug("Failed to inspect PyTorch CUDA availability", exc_info=True)
        return False


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
    
    # Model mapping for config compatibility. Do not remap BGE multilingual
    # rerankers to English MS MARCO fallbacks.
    MODEL_MAPPING = {
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
        self.configured_device = getattr(config, "device", "cuda")
        self.effective_device = self.configured_device
        self.fallback_reason: Optional[str] = None
        
        # Map model name if needed
        self._model_name = self.MODEL_MAPPING.get(
            config.model, 
            config.model
        )
        
        # Check if we need to use a fallback
        if self._model_name != config.model:
            logger.info(f"Using fallback model: {self._model_name} instead of {config.model}")

    def _resolve_device(self) -> Optional[str]:
        """Resolve the configured device against the active PyTorch runtime.

        Explicit CPU and other non-CUDA device values are passed through
        without probing CUDA.  CUDA values are retained when available and
        otherwise use CPU when the backward-compatible fallback is enabled.
        """
        configured_device = self.configured_device
        normalized_device = str(configured_device or "").strip().lower()
        self.fallback_reason = None
        self.effective_device = configured_device

        if not normalized_device.startswith("cuda"):
            logger.info(
                "Reranker device resolved: configured=%s effective=%s",
                configured_device,
                self.effective_device,
            )
            return self.effective_device

        if _is_cuda_available():
            logger.info(
                "Reranker device resolved: configured=%s effective=%s",
                configured_device,
                self.effective_device,
            )
            return self.effective_device

        fallback_enabled = bool(
            getattr(self.config, "allow_cpu_fallback", True)
        )
        if fallback_enabled:
            self.effective_device = "cpu"
            self.fallback_reason = (
                "configured CUDA device is unavailable "
                "(torch.cuda.is_available() is false)"
            )
            logger.warning(
                "Reranker device fallback: configured=%s effective=%s reason=%s",
                configured_device,
                self.effective_device,
                self.fallback_reason,
            )
            return self.effective_device

        self.effective_device = None
        self.fallback_reason = (
            "configured CUDA device is unavailable and CPU fallback is disabled"
        )
        logger.error(
            "Reranker device unavailable: configured=%s effective=None reason=%s",
            configured_device,
            self.fallback_reason,
        )
        return None
    
    async def initialize(self) -> bool:
        """Initialize the reranker model.
        
        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True

        effective_device = self._resolve_device()
        if effective_device is None:
            self.model = None
            self._initialized = False
            return False
        
        CrossEncoder = _load_cross_encoder()
        if CrossEncoder is None:
            self.model = None
            self._initialized = False
            return False
        
        try:
            logger.info(f"Loading reranker model: {self._model_name}")
            
            loop = asyncio.get_event_loop()
            
            def _load_model():
                return CrossEncoder(
                    self._model_name,
                    device=effective_device
                )
            
            self.model = await loop.run_in_executor(self._executor, _load_model)
            
            self._initialized = True
            logger.info(
                "Reranker model loaded successfully: configured_device=%s "
                "effective_device=%s",
                self.configured_device,
                self.effective_device,
            )
            return True
            
        except Exception as e:
            self.model = None
            self._initialized = False
            logger.error(
                "Failed to load reranker model (configured_device=%s "
                "effective_device=%s): %s",
                self.configured_device,
                self.effective_device,
                e,
            )
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
