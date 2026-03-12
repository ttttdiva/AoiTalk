"""
Embedding generation and management for conversation memory
"""

import json
import asyncio
import os
from typing import List, Optional, Union
from sentence_transformers import SentenceTransformer
import numpy as np


class EmbeddingManager:
    """Manages text embeddings for semantic search"""
    
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """Initialize embedding manager
        
        Args:
            model_name: Name of the sentence transformer model
        """
        self.model_name = model_name
        self.model: Optional[SentenceTransformer] = None
        self._lock = asyncio.Lock()
    
    async def _load_model(self):
        """Load the embedding model (thread-safe)"""
        async with self._lock:
            if self.model is None:
                print(f"[EmbeddingManager] Loading model: {self.model_name}")
                loop = asyncio.get_event_loop()
                # Use default cache location to avoid re-downloading
                self.model = await loop.run_in_executor(
                    None, SentenceTransformer, self.model_name
                )
                print(f"[EmbeddingManager] Model loaded successfully")
    
    async def preload_model(self):
        """Preload the embedding model for faster first-use"""
        if self.model is None:
            await self._load_model()
    
    async def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding for a text
        
        Args:
            text: Input text
            
        Returns:
            List[float]: Embedding vector
        """
        if not text or not text.strip():
            return []
        
        if self.model is None:
            await self._load_model()
        
        try:
            # Generate embedding in executor to avoid blocking
            loop = asyncio.get_event_loop()
            embedding = await loop.run_in_executor(
                None, self.model.encode, text
            )
            return embedding.tolist()
            
        except Exception as e:
            print(f"[EmbeddingManager] Error generating embedding: {e}")
            return []
    
    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts
        
        Args:
            texts: List of input texts
            
        Returns:
            List[List[float]]: List of embedding vectors
        """
        if not texts:
            return []
        
        if self.model is None:
            await self._load_model()
        
        try:
            # Filter out empty texts
            valid_texts = [text for text in texts if text and text.strip()]
            if not valid_texts:
                return []
            
            # Generate embeddings in executor
            loop = asyncio.get_event_loop()
            embeddings = await loop.run_in_executor(
                None, self.model.encode, valid_texts
            )
            return [emb.tolist() for emb in embeddings]
            
        except Exception as e:
            print(f"[EmbeddingManager] Error generating embeddings: {e}")
            return []
    
    def calculate_similarity(self, embedding1: List[float], embedding2: List[float]) -> float:
        """Calculate cosine similarity between two embeddings
        
        Args:
            embedding1: First embedding vector
            embedding2: Second embedding vector
            
        Returns:
            float: Cosine similarity score (0-1)
        """
        if not embedding1 or not embedding2:
            return 0.0
        
        try:
            vec1 = np.array(embedding1)
            vec2 = np.array(embedding2)
            
            # Calculate cosine similarity
            dot_product = np.dot(vec1, vec2)
            norm1 = np.linalg.norm(vec1)
            norm2 = np.linalg.norm(vec2)
            
            if norm1 == 0 or norm2 == 0:
                return 0.0
            
            similarity = dot_product / (norm1 * norm2)
            return float(similarity)
            
        except Exception as e:
            print(f"[EmbeddingManager] Error calculating similarity: {e}")
            return 0.0
    
    @staticmethod
    def serialize_embedding(embedding: List[float]) -> str:
        """Serialize embedding to JSON string for database storage
        
        Args:
            embedding: Embedding vector
            
        Returns:
            str: JSON string representation
        """
        if not embedding:
            return ""
        return json.dumps(embedding)
    
    @staticmethod
    def deserialize_embedding(embedding_str: str) -> List[float]:
        """Deserialize embedding from JSON string
        
        Args:
            embedding_str: JSON string representation
            
        Returns:
            List[float]: Embedding vector
        """
        if not embedding_str:
            return []
        
        try:
            return json.loads(embedding_str)
        except (json.JSONDecodeError, TypeError):
            return []


# Global embedding manager instance
_embedding_manager: Optional[EmbeddingManager] = None


def get_embedding_manager(model_name: str = "all-MiniLM-L6-v2") -> EmbeddingManager:
    """Get global embedding manager instance
    
    Args:
        model_name: Embedding model name (only used on first call)
        
    Returns:
        EmbeddingManager: Global embedding manager
    """
    global _embedding_manager
    
    if _embedding_manager is None:
        _embedding_manager = EmbeddingManager(model_name)
    elif _embedding_manager.model_name != model_name:
        # Log warning if trying to use different model
        print(f"[EmbeddingManager] Warning: Requested model '{model_name}' differs from loaded model '{_embedding_manager.model_name}'. Using existing model.")
    
    return _embedding_manager