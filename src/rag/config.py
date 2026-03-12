"""
RAG configuration management.
"""

import os
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
import yaml


DEFAULT_COLLECTION_NAME = "aoitalk_documents"


def get_project_collection_name(project_id: Optional[str] = None) -> str:
    """Get collection name for a project.
    
    Args:
        project_id: Project UUID string. If None, returns default collection.
        
    Returns:
        Collection name: 'project_{id}_documents' or 'aoitalk_documents'
    """
    if project_id:
        return f"project_{project_id}_documents"
    return DEFAULT_COLLECTION_NAME


@dataclass
class QdrantConfig:
    """Qdrant connection configuration."""
    host: str = "localhost"
    port: int = 6333
    collection_name: str = DEFAULT_COLLECTION_NAME
    api_key: Optional[str] = None
    local_path: Optional[str] = None  # If set, use local file storage instead of server
    
    def get_collection_name_for_project(self, project_id: Optional[str] = None) -> str:
        """Get collection name for a specific project.
        
        Args:
            project_id: Project UUID string. If None, returns default collection.
            
        Returns:
            Collection name
        """
        return get_project_collection_name(project_id)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'QdrantConfig':
        return cls(
            host=data.get('host', cls.host),
            port=data.get('port', cls.port),
            collection_name=data.get('collection_name', DEFAULT_COLLECTION_NAME),
            api_key=data.get('api_key'),
            local_path=data.get('local_path')  # None = use server, path = local file storage
        )


@dataclass
class EmbeddingConfig:
    """Embedding model configuration."""
    model: str = "BAAI/bge-m3"
    batch_size: int = 32
    device: str = "cuda"  # cuda or cpu
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'EmbeddingConfig':
        return cls(
            model=data.get('model', cls.model),
            batch_size=data.get('batch_size', cls.batch_size),
            device=data.get('device', cls.device)
        )


@dataclass
class RerankerConfig:
    """Reranker model configuration."""
    model: str = "BAAI/bge-reranker-v2-gemma"
    top_n: int = 5
    device: str = "cuda"
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RerankerConfig':
        return cls(
            model=data.get('model', cls.model),
            top_n=data.get('top_n', cls.top_n),
            device=data.get('device', cls.device)
        )


@dataclass
class ChunkingConfig:
    """Document chunking configuration."""
    chunk_size: int = 512
    chunk_overlap: int = 50
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ChunkingConfig':
        return cls(
            chunk_size=data.get('chunk_size', cls.chunk_size),
            chunk_overlap=data.get('chunk_overlap', cls.chunk_overlap)
        )


@dataclass
class SearchConfig:
    """Search configuration."""
    top_k: int = 20  # Initial retrieval count from Qdrant
    top_n: int = 5   # Final count after reranking
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SearchConfig':
        return cls(
            top_k=data.get('top_k', cls.top_k),
            top_n=data.get('top_n', cls.top_n)
        )


@dataclass
class IndexingConfig:
    """Indexing performance configuration."""
    batch_size: int = 64  # Number of chunks to embed at once
    max_concurrent_files: int = 10  # Max files to read concurrently
    progress_interval: int = 100  # Log progress every N chunks
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'IndexingConfig':
        return cls(
            batch_size=data.get('batch_size', cls.batch_size),
            max_concurrent_files=data.get('max_concurrent_files', cls.max_concurrent_files),
            progress_interval=data.get('progress_interval', cls.progress_interval)
        )


@dataclass
class SourceConfig:
    """Source document configuration."""
    directories: List[str] = field(default_factory=list)
    include_patterns: List[str] = field(default_factory=lambda: ["*.md", "*.txt", "*.pdf"])
    exclude_patterns: List[str] = field(default_factory=lambda: [".*", "__pycache__"])
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SourceConfig':
        # Default values for list fields
        default_include = ["*.md", "*.txt", "*.pdf"]
        default_exclude = [".*", "__pycache__"]
        
        return cls(
            directories=data.get('directories', []),
            include_patterns=data.get('include_patterns', default_include),
            exclude_patterns=data.get('exclude_patterns', default_exclude)
        )


@dataclass
class RagConfig:
    """Main RAG configuration."""
    enabled: bool = False
    qdrant: QdrantConfig = field(default_factory=QdrantConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    indexing: IndexingConfig = field(default_factory=IndexingConfig)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RagConfig':
        return cls(
            enabled=data.get('enabled', False),
            qdrant=QdrantConfig.from_dict(data.get('qdrant', {})),
            embedding=EmbeddingConfig.from_dict(data.get('embedding', {})),
            reranker=RerankerConfig.from_dict(data.get('reranker', {})),
            chunking=ChunkingConfig.from_dict(data.get('chunking', {})),
            search=SearchConfig.from_dict(data.get('search', {})),
            source=SourceConfig.from_dict(data.get('source', {})),
            indexing=IndexingConfig.from_dict(data.get('indexing', {}))
        )
    
    @classmethod
    def from_yaml(cls, config_path: str) -> 'RagConfig':
        """Load configuration from YAML file."""
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        
        rag_config = config_data.get('rag', {})
        return cls.from_dict(rag_config)


# Global config instance
_rag_config: Optional[RagConfig] = None


def get_rag_config(config_path: Optional[str] = None) -> RagConfig:
    """Get or create global RAG config instance.
    
    Args:
        config_path: Path to config.yaml (only used on first call)
        
    Returns:
        RagConfig instance
    """
    global _rag_config
    
    if _rag_config is None:
        if config_path is None:
            # Default config path
            config_path = str(Path(__file__).parents[2] / "config" / "config.yaml")
        
        if Path(config_path).exists():
            _rag_config = RagConfig.from_yaml(config_path)
        else:
            # Use defaults
            _rag_config = RagConfig()
    
    return _rag_config
