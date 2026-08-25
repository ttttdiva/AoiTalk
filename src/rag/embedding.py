"""
Embedding model for RAG using sentence-transformers.

Uses BGE-M3 or compatible models via sentence-transformers library.
"""

import logging
import asyncio
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor

from .config import EmbeddingConfig

logger = logging.getLogger(__name__)

# Lazy imports
_SentenceTransformer = None


def _is_cuda_available() -> bool:
    """Return whether the active PyTorch runtime can use CUDA.

    PyTorch is intentionally imported lazily because the RAG embedding path is
    optional.  A missing/broken CPU-only torch install is treated as CUDA
    unavailable; the caller decides whether that should fall back to CPU or
    fail without constructing the model.
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


def _load_sentence_transformer():
    """Lazy load sentence-transformers library."""
    global _SentenceTransformer
    if _SentenceTransformer is None:
        try:
            from sentence_transformers import SentenceTransformer
            _SentenceTransformer = SentenceTransformer
        except ImportError:
            logger.warning("sentence-transformers not installed. Embedding features will be disabled.")
    return _SentenceTransformer


class BgeM3Embedding:
    """Embedding model wrapper using sentence-transformers.
    
    Supports any sentence-transformers compatible model including:
    - BAAI/bge-m3
    - intfloat/multilingual-e5-large
    - sentence-transformers/all-MiniLM-L6-v2
    """
    
    def __init__(self, config: EmbeddingConfig):
        """Initialize embedding model.
        
        Args:
            config: Embedding configuration
        """
        self.config = config
        self.model = None
        self._initialized = False
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._dimension = 1024  # Default for bge-m3
        self.configured_device = getattr(config, "device", "cuda")
        self.effective_device = self.configured_device
        self.fallback_reason: Optional[str] = None

    def _resolve_device(self) -> Optional[str]:
        """Resolve the configured device against the active PyTorch runtime.

        ``sentence-transformers`` accepts arbitrary device strings, so only
        CUDA devices need capability detection.  Keeping the configured value
        unchanged preserves existing configuration/API behavior while exposing
        the effective value used for the model constructor.
        """
        configured_device = self.configured_device
        normalized_device = str(configured_device or "").strip().lower()
        self.fallback_reason = None
        self.effective_device = configured_device

        if not normalized_device.startswith("cuda"):
            logger.info(
                "Embedding device resolved: configured=%s effective=%s",
                configured_device,
                self.effective_device,
            )
            return self.effective_device

        if _is_cuda_available():
            logger.info(
                "Embedding device resolved: configured=%s effective=%s",
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
                "Embedding device fallback: configured=%s effective=%s reason=%s",
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
            "Embedding device unavailable: configured=%s effective=None reason=%s",
            configured_device,
            self.fallback_reason,
        )
        return None
    
    async def initialize(self) -> bool:
        """Initialize the embedding model.
        
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
        
        SentenceTransformer = _load_sentence_transformer()
        if SentenceTransformer is None:
            self.model = None
            self._initialized = False
            return False
        
        try:
            logger.info(f"Loading embedding model: {self.config.model}")
            
            loop = asyncio.get_event_loop()
            
            def _load_model():
                model = SentenceTransformer(
                    self.config.model,
                    device=effective_device
                )
                return model
            
            self.model = await loop.run_in_executor(self._executor, _load_model)
            
            # Get actual embedding dimension
            self._dimension = self.model.get_sentence_embedding_dimension()
            
            self._initialized = True
            logger.info(
                "Embedding model loaded: dim=%s configured_device=%s effective_device=%s",
                self._dimension,
                self.configured_device,
                self.effective_device,
            )
            return True
            
        except Exception as e:
            self.model = None
            self._initialized = False
            logger.error(
                "Failed to load embedding model (configured_device=%s "
                "effective_device=%s): %s",
                self.configured_device,
                self.effective_device,
                e,
            )
            return False
    
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for texts.
        
        Args:
            texts: List of texts to embed
            
        Returns:
            List of embedding vectors
        """
        if not self._initialized or self.model is None:
            logger.error("Embedding model not initialized")
            return []
        
        if not texts:
            return []
        
        try:
            loop = asyncio.get_event_loop()
            
            def _encode_batch(batch):
                embeddings = self.model.encode(
                    batch,
                    batch_size=self.config.batch_size,
                    show_progress_bar=False,
                    convert_to_numpy=True
                )
                return embeddings.tolist()
            
            # Process in batches
            all_embeddings = []
            for i in range(0, len(texts), self.config.batch_size):
                batch = texts[i:i + self.config.batch_size]
                
                batch_embeddings = await loop.run_in_executor(
                    self._executor,
                    lambda b=batch: _encode_batch(b)
                )
                all_embeddings.extend(batch_embeddings)
            
            return all_embeddings
            
        except Exception as e:
            logger.error(f"Failed to generate embeddings: {e}")
            return []
    
    async def embed_query(self, query: str) -> Optional[List[float]]:
        """Generate embedding for a single query.
        
        Args:
            query: Query text
            
        Returns:
            Embedding vector or None
        """
        embeddings = await self.embed([query])
        return embeddings[0] if embeddings else None
    
    @property
    def dimension(self) -> int:
        """Get embedding dimension.
        
        Returns:
            Embedding vector dimension
        """
        return self._dimension
    
    def close(self):
        """Cleanup resources."""
        self._executor.shutdown(wait=False)
        self.model = None
        self._initialized = False
