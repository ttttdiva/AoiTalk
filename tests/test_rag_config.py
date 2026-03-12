"""RAG設定モジュールのテスト"""
import pytest


class TestGetProjectCollectionName:
    def test_with_project_id(self):
        from src.rag.config import get_project_collection_name

        result = get_project_collection_name("abc-123")
        assert result == "project_abc-123_documents"

    def test_without_project_id(self):
        from src.rag.config import get_project_collection_name

        assert get_project_collection_name(None) == "aoitalk_documents"
        assert get_project_collection_name("") == "aoitalk_documents"


class TestQdrantConfig:
    def test_defaults(self):
        from src.rag.config import QdrantConfig

        cfg = QdrantConfig()
        assert cfg.host == "localhost"
        assert cfg.port == 6333
        assert cfg.collection_name == "aoitalk_documents"
        assert cfg.api_key is None

    def test_from_dict(self):
        from src.rag.config import QdrantConfig

        cfg = QdrantConfig.from_dict({"host": "myhost", "port": 9999})
        assert cfg.host == "myhost"
        assert cfg.port == 9999

    def test_from_dict_empty(self):
        from src.rag.config import QdrantConfig

        cfg = QdrantConfig.from_dict({})
        assert cfg.host == "localhost"

    def test_get_collection_name_for_project(self):
        from src.rag.config import QdrantConfig

        cfg = QdrantConfig()
        assert cfg.get_collection_name_for_project("proj1") == "project_proj1_documents"
        assert cfg.get_collection_name_for_project(None) == "aoitalk_documents"


class TestEmbeddingConfig:
    def test_defaults(self):
        from src.rag.config import EmbeddingConfig

        cfg = EmbeddingConfig()
        assert cfg.model == "BAAI/bge-m3"
        assert cfg.batch_size == 32

    def test_from_dict(self):
        from src.rag.config import EmbeddingConfig

        cfg = EmbeddingConfig.from_dict({"model": "custom/model", "device": "cpu"})
        assert cfg.model == "custom/model"
        assert cfg.device == "cpu"


class TestRerankerConfig:
    def test_defaults(self):
        from src.rag.config import RerankerConfig

        cfg = RerankerConfig()
        assert cfg.top_n == 5

    def test_from_dict(self):
        from src.rag.config import RerankerConfig

        cfg = RerankerConfig.from_dict({"top_n": 10})
        assert cfg.top_n == 10


class TestChunkingConfig:
    def test_defaults(self):
        from src.rag.config import ChunkingConfig

        cfg = ChunkingConfig()
        assert cfg.chunk_size == 512
        assert cfg.chunk_overlap == 50

    def test_from_dict(self):
        from src.rag.config import ChunkingConfig

        cfg = ChunkingConfig.from_dict({"chunk_size": 256, "chunk_overlap": 25})
        assert cfg.chunk_size == 256
        assert cfg.chunk_overlap == 25


class TestSearchConfig:
    def test_defaults(self):
        from src.rag.config import SearchConfig

        cfg = SearchConfig()
        assert cfg.top_k == 20
        assert cfg.top_n == 5


class TestIndexingConfig:
    def test_defaults(self):
        from src.rag.config import IndexingConfig

        cfg = IndexingConfig()
        assert cfg.batch_size == 64

    def test_from_dict(self):
        from src.rag.config import IndexingConfig

        cfg = IndexingConfig.from_dict({"batch_size": 128})
        assert cfg.batch_size == 128


class TestSourceConfig:
    def test_defaults(self):
        from src.rag.config import SourceConfig

        cfg = SourceConfig()
        assert "*.md" in cfg.include_patterns
        assert "*.txt" in cfg.include_patterns
        assert cfg.directories == []

    def test_from_dict(self):
        from src.rag.config import SourceConfig

        cfg = SourceConfig.from_dict({"directories": ["/data"], "include_patterns": ["*.py"]})
        assert cfg.directories == ["/data"]
        assert cfg.include_patterns == ["*.py"]


class TestRagConfig:
    def test_defaults(self):
        from src.rag.config import RagConfig

        cfg = RagConfig()
        assert cfg.enabled is False
        assert cfg.qdrant.host == "localhost"
        assert cfg.chunking.chunk_size == 512

    def test_from_dict_full(self):
        from src.rag.config import RagConfig

        data = {
            "enabled": True,
            "qdrant": {"host": "remote-host", "port": 6334},
            "embedding": {"model": "custom/emb"},
            "chunking": {"chunk_size": 1024},
            "search": {"top_k": 50},
        }
        cfg = RagConfig.from_dict(data)
        assert cfg.enabled is True
        assert cfg.qdrant.host == "remote-host"
        assert cfg.embedding.model == "custom/emb"
        assert cfg.chunking.chunk_size == 1024
        assert cfg.search.top_k == 50

    def test_from_dict_empty(self):
        from src.rag.config import RagConfig

        cfg = RagConfig.from_dict({})
        assert cfg.enabled is False
        assert cfg.qdrant.host == "localhost"
