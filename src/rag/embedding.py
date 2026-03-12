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
    
    async def initialize(self) -> bool:
        """Initialize the embedding model.
        
        Returns:
            True if initialization successful
        """
        if self._initialized:
            return True
        
        SentenceTransformer = _load_sentence_transformer()
        if SentenceTransformer is None:
            return False
        
        try:
            logger.info(f"Loading embedding model: {self.config.model}")
            
            loop = asyncio.get_event_loop()
            
            def _load_model():
                model = SentenceTransformer(
                    self.config.model,
                    device=self.config.device
                )
                return model
            
            self.model = await loop.run_in_executor(self._executor, _load_model)
            
            # Get actual embedding dimension
            self._dimension = self.model.get_sentence_embedding_dimension()
            
            self._initialized = True
            logger.info(f"Embedding model loaded: dim={self._dimension}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
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
