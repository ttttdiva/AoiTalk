"""memoryモジュールのテスト（外部サービス不要な部分）"""
import json
import pytest
import numpy as np


class TestMemoryConfig:
    def test_defaults(self):
        from src.memory.config import MemoryConfig

        cfg = MemoryConfig()
        assert cfg.max_active_messages == 50
        assert cfg.summary_overlap == 5
        assert cfg.similarity_threshold == 0.3
        assert cfg.exclude_patterns == []

    def test_max_active_messages_minimum(self):
        from src.memory.config import MemoryConfig

        with pytest.raises(ValueError, match="max_active_messages must be at least 5"):
            MemoryConfig(max_active_messages=3)

    def test_summary_overlap_less_than_max(self):
        from src.memory.config import MemoryConfig

        with pytest.raises(
            ValueError, match="summary_overlap must be less than max_active_messages"
        ):
            MemoryConfig(max_active_messages=10, summary_overlap=15)

    def test_similarity_threshold_bounds(self):
        from src.memory.config import MemoryConfig

        with pytest.raises(ValueError, match="similarity_threshold must be between"):
            MemoryConfig(similarity_threshold=1.5)
        with pytest.raises(ValueError, match="similarity_threshold must be between"):
            MemoryConfig(similarity_threshold=-0.1)

    def test_log_retention_days_non_negative(self):
        from src.memory.config import MemoryConfig

        with pytest.raises(ValueError, match="log_retention_days must be non-negative"):
            MemoryConfig(log_retention_days=-1)

    def test_exclude_patterns_none_becomes_list(self):
        from src.memory.config import MemoryConfig

        cfg = MemoryConfig(exclude_patterns=None)
        assert cfg.exclude_patterns == []


class TestEmbeddingManagerPureFunctions:
    """EmbeddingManagerの純粋ロジック（モデルロード不要な部分）"""

    def test_calculate_similarity_orthogonal(self):
        from src.memory.embedding import EmbeddingManager

        mgr = EmbeddingManager()
        sim = mgr.calculate_similarity([1.0, 0.0, 0.0], [0.0, 1.0, 0.0])
        assert abs(sim) < 1e-6

    def test_calculate_similarity_identical(self):
        from src.memory.embedding import EmbeddingManager

        mgr = EmbeddingManager()
        vec = [0.5, 0.5, 0.5]
        sim = mgr.calculate_similarity(vec, vec)
        assert abs(sim - 1.0) < 1e-6

    def test_calculate_similarity_opposite(self):
        from src.memory.embedding import EmbeddingManager

        mgr = EmbeddingManager()
        sim = mgr.calculate_similarity([1.0, 0.0], [-1.0, 0.0])
        assert abs(sim - (-1.0)) < 1e-6

    def test_calculate_similarity_empty(self):
        from src.memory.embedding import EmbeddingManager

        mgr = EmbeddingManager()
        assert mgr.calculate_similarity([], [1.0, 0.0]) == 0.0
        assert mgr.calculate_similarity([1.0, 0.0], []) == 0.0

    def test_calculate_similarity_zero_vector(self):
        from src.memory.embedding import EmbeddingManager

        mgr = EmbeddingManager()
        assert mgr.calculate_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_serialize_deserialize_roundtrip(self):
        from src.memory.embedding import EmbeddingManager

        original = [0.1, 0.2, 0.3, -0.5]
        serialized = EmbeddingManager.serialize_embedding(original)
        deserialized = EmbeddingManager.deserialize_embedding(serialized)
        assert deserialized == original

    def test_serialize_empty(self):
        from src.memory.embedding import EmbeddingManager

        assert EmbeddingManager.serialize_embedding([]) == ""

    def test_deserialize_empty(self):
        from src.memory.embedding import EmbeddingManager

        assert EmbeddingManager.deserialize_embedding("") == []

    def test_deserialize_invalid_json(self):
        from src.memory.embedding import EmbeddingManager

        assert EmbeddingManager.deserialize_embedding("not json") == []


class TestFeedbackRepository:
    def test_generate_id_format(self):
        from src.memory.feedback_repository import FeedbackRepository

        fid = FeedbackRepository.generate_id()
        assert fid.startswith("fb_")
        parts = fid.split("_")
        assert len(parts) == 3
        # タイムスタンプ部分は数字
        assert parts[1].isdigit()
        # UUID部分は8文字の16進数
        assert len(parts[2]) == 8

    def test_generate_id_unique(self):
        from src.memory.feedback_repository import FeedbackRepository

        ids = {FeedbackRepository.generate_id() for _ in range(100)}
        assert len(ids) == 100


class TestCrossSessionMemory:
    def test_should_search_japanese_keywords(self):
        from src.memory.cross_session_memory import CrossSessionMemoryService

        svc = CrossSessionMemoryService()
        assert svc.should_search_past_conversations("前に教えた内容は？") is True
        assert svc.should_search_past_conversations("以前の話なんだけど") is True
        assert svc.should_search_past_conversations("覚えてる？") is True
        assert svc.should_search_past_conversations("約束したよね") is True

    def test_should_search_english_keywords(self):
        from src.memory.cross_session_memory import CrossSessionMemoryService

        svc = CrossSessionMemoryService()
        assert svc.should_search_past_conversations("remember what I told you?") is True
        assert svc.should_search_past_conversations("as mentioned before") is True

    def test_should_search_pronoun_patterns(self):
        from src.memory.cross_session_memory import CrossSessionMemoryService

        svc = CrossSessionMemoryService()
        assert svc.should_search_past_conversations("それって") is True
        assert svc.should_search_past_conversations("あれ？") is True

    def test_should_not_search_normal(self):
        from src.memory.cross_session_memory import CrossSessionMemoryService

        svc = CrossSessionMemoryService()
        assert svc.should_search_past_conversations("こんにちは") is False
        assert svc.should_search_past_conversations("今日の天気は？") is False

    def test_should_not_search_empty(self):
        from src.memory.cross_session_memory import CrossSessionMemoryService

        svc = CrossSessionMemoryService()
        assert svc.should_search_past_conversations("") is False
        assert svc.should_search_past_conversations("a") is False

    def test_format_memory_context_empty(self):
        from src.memory.cross_session_memory import CrossSessionMemoryService

        svc = CrossSessionMemoryService()
        assert svc.format_memory_context([]) == ""

    def test_format_memory_context_basic(self):
        from src.memory.cross_session_memory import CrossSessionMemoryService

        svc = CrossSessionMemoryService()
        results = [
            {"role": "user", "content": "テスト内容", "relevance_score": 0.8},
            {"role": "assistant", "content": "応答内容", "relevance_score": 0.6},
        ]
        context = svc.format_memory_context(results)
        assert "過去の会話からの関連情報" in context
        assert "ユーザー" in context
        assert "あなた" in context
        assert "0.80" in context

    def test_format_memory_context_max_chars(self):
        from src.memory.cross_session_memory import CrossSessionMemoryService

        svc = CrossSessionMemoryService()
        results = [
            {"role": "user", "content": "x" * 500, "relevance_score": 0.9}
            for _ in range(10)
        ]
        context = svc.format_memory_context(results, max_chars=200)
        assert len(context) <= 500  # ヘッダー含めて余裕
