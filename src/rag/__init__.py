"""
RAG (Retrieval Augmented Generation) module for AoiTalk.

This module provides document retrieval capabilities using:
- Qdrant: Vector database for similarity search
- BAAI/bge-m3: Embedding model
- BAAI/bge-reranker-v2-gemma: Reranking model  
- LlamaIndex: Document processing
"""

from .config import RagConfig, get_rag_config
from .manager import RagManager, get_rag_manager

__all__ = [
    'RagConfig',
    'get_rag_config',
    'RagManager', 
    'get_rag_manager',
]
