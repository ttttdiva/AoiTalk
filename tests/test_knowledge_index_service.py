"""Knowledge Index service tests."""

from __future__ import annotations

import uuid

import pytest

from src.knowledge import index_service as index_module


pytestmark = pytest.mark.skipif(
    not index_module.QDRANT_AVAILABLE,
    reason="qdrant-client is required for Knowledge Index tests",
)


def test_hashing_sparse_encoder_is_deterministic_for_exact_terms():
    encoder = index_module.HashingSparseEncoder()

    first = encoder.encode("ExampleGateway 192.0.2.10 設計資料")
    second = encoder.encode("ExampleGateway 192.0.2.10 設計資料")

    assert first.indices == second.indices
    assert first.values == second.values
    assert first.indices


def test_knowledge_index_payload_uses_stable_knowledge_ids():
    from src.memory.models import KnowledgeChunk, KnowledgeDocument, KnowledgeSource
    from src.rag.config import RagConfig

    source = KnowledgeSource.__new__(KnowledgeSource)
    source.id = uuid.uuid4()
    source.name = "案件Workspace"
    source.source_type = "project_workspace"

    document = KnowledgeDocument.__new__(KnowledgeDocument)
    document.id = uuid.uuid4()
    document.title = "基本設計書"
    document.path = "01_設計/basic.md"
    document.extension = ".md"
    document.status = "active"
    document.tags = ["design"]
    document.project_refs = ["project-1"]
    document.task_refs = ["task-1"]

    chunk = KnowledgeChunk.__new__(KnowledgeChunk)
    chunk.id = uuid.uuid4()
    chunk.text = "ExampleGatewayのVLAN設計"
    chunk.heading_path = ["ネットワーク"]
    chunk.chunk_index = 2
    chunk.content_hash = "abc"

    service = index_module.KnowledgeIndexService(config=RagConfig(enabled=False))
    payload = service._payload(chunk, document, source)

    assert payload["chunk_id"] == str(chunk.id)
    assert payload["document_id"] == str(document.id)
    assert payload["source_id"] == str(source.id)
    assert payload["project_refs"] == ["project-1"]
    assert payload["task_refs"] == ["task-1"]
    assert payload["source_type"] == "project_workspace"
