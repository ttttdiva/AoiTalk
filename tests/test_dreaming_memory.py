"""Dreaming memory extraction and normalization tests."""


def test_dreaming_extractor_parses_structured_candidates():
    from src.memory.dreaming_extractor import DreamingMemoryExtractor

    output = """
    ```json
    [
      {
        "content": "The user prefers concise Japanese answers.",
        "memory_type": "preference",
        "confidence": 0.9,
        "importance": 8
      },
      "The user often works with TypeScript."
    ]
    ```
    """

    memories = DreamingMemoryExtractor()._parse_response(output)

    assert memories is not None
    assert memories[0]["content"] == "The user prefers concise Japanese answers."
    assert memories[0]["memory_type"] == "preference"
    assert memories[1]["content"] == "The user often works with TypeScript."
    assert memories[1]["memory_type"] == "fact"


def test_dreaming_candidate_normalization_rejects_empty_and_unknown_types():
    from src.services.dreaming_memory_service import _normalize_candidate

    assert _normalize_candidate("") is None
    normalized = _normalize_candidate(
        {
            "content": "The user wants active project context to be prioritized.",
            "memory_type": "unknown",
            "confidence": 2,
            "importance": 99,
        }
    )

    assert normalized is not None
    assert normalized["memory_type"] == "fact"
    assert normalized["confidence"] == 1.0
    assert normalized["importance"] == 10
