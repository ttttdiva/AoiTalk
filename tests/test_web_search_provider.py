import importlib
import pytest

from src.services.deep_research_service import DeepResearchSource
from src.services.quick_search_service import (
    get_search_provider,
    local_web_search_async,
    normalize_local_search_query,
)


web_search_module = importlib.import_module("src.tools.basic.web_search")


class DummyConfig:
    def __init__(self, provider: str):
        self.provider = provider

    def get(self, key, default=None):
        if key == "search.provider":
            return self.provider
        return default


def test_search_provider_defaults_to_openai_for_unknown_value():
    assert get_search_provider({"search": {"provider": "local"}}) == "local"
    assert get_search_provider({"search": {"provider": "unknown"}}) == "openai"
    assert get_search_provider({}) == "openai"


def test_local_provider_skips_external_permission(monkeypatch):
    def fail_permission(*_args, **_kwargs):
        raise AssertionError("local search should not ask external permission")

    monkeypatch.setattr(web_search_module, "check_permission_sync", fail_permission)
    monkeypatch.setattr(
        web_search_module,
        "local_web_search_impl",
        lambda query, config=None: f"local:{query}",
    )

    assert (
        web_search_module.web_search_with_config("AoiTalk", DummyConfig("local"))
        == "local:AoiTalk"
    )


def test_normalize_local_search_query_removes_japanese_command_text():
    assert (
        normalize_local_search_query(
            "\u30a2\u30af\u30a2\u30de\u30ea\u30f3\u306e\u30e2\u30fc\u30b9\u786c\u5ea6\u3092\u8abf\u3079\u3066\u304f\u3060\u3055\u3044\u3002"
        )
        == "\u30a2\u30af\u30a2\u30de\u30ea\u30f3\u306e\u30e2\u30fc\u30b9\u786c\u5ea6"
    )
    assert (
        normalize_local_search_query(
            "\u5ff5\u306e\u305f\u3081\u691c\u7d22\u3057\u3066\u3001\u30a2\u30af\u30a2\u30de\u30ea\u30f3\u306e\u30e2\u30fc\u30b9\u786c\u5ea6"
        )
        == "\u30a2\u30af\u30a2\u30de\u30ea\u30f3\u306e\u30e2\u30fc\u30b9\u786c\u5ea6"
    )


@pytest.mark.asyncio
async def test_local_search_uses_normalized_query(monkeypatch):
    seen = []

    class FakeSearchClient:
        def __init__(self, config=None, timeout_seconds=10.0):
            pass

        async def search(self, query, **kwargs):
            seen.append(query)
            return [
                DeepResearchSource(
                    id=1,
                    title="Aquamarine",
                    url="https://example.test/aquamarine",
                    snippet="Mohs hardness 7.5",
                    engine="fake",
                    query=query,
                )
            ]

    quick_search_module = importlib.import_module("src.services.quick_search_service")
    monkeypatch.setattr(
        quick_search_module,
        "DeepResearchSearchClient",
        FakeSearchClient,
    )

    result = await local_web_search_async(
        "\u30a2\u30af\u30a2\u30de\u30ea\u30f3\u306e\u30e2\u30fc\u30b9\u786c\u5ea6\u3092\u8abf\u3079\u3066\u304f\u3060\u3055\u3044\u3002",
        {"search": {"provider": "local"}},
    )

    assert seen == ["\u30a2\u30af\u30a2\u30de\u30ea\u30f3\u306e\u30e2\u30fc\u30b9\u786c\u5ea6"]
    assert "汎用Web検索結果" in result
    assert "\u30a2\u30af\u30a2\u30de\u30ea\u30f3\u306e\u30e2\u30fc\u30b9\u786c\u5ea6" in result


@pytest.mark.asyncio
async def test_local_search_uses_generic_web_search_label_when_empty(monkeypatch):
    class FakeSearchClient:
        def __init__(self, config=None, timeout_seconds=10.0):
            pass

        async def search(self, query, **kwargs):
            return []

    quick_search_module = importlib.import_module("src.services.quick_search_service")
    monkeypatch.setattr(
        quick_search_module,
        "DeepResearchSearchClient",
        FakeSearchClient,
    )

    result = await local_web_search_async("AoiTalk", {"search": {"provider": "local"}})

    assert "汎用Web検索結果は見つかりませんでした" in result
    assert "ローカル検索" not in result


def test_openai_provider_uses_external_permission(monkeypatch):
    seen = {}

    def fake_permission(tool_name, tool_args, description):
        seen["tool_name"] = tool_name
        seen["tool_args"] = tool_args
        seen["description"] = description
        return False

    def fail_openai(*_args, **_kwargs):
        raise AssertionError("denied OpenAI search should not execute")

    monkeypatch.setattr(web_search_module, "check_permission_sync", fake_permission)
    monkeypatch.setattr(web_search_module, "openai_web_search_impl", fail_openai)

    result = web_search_module.web_search_with_config(
        "AoiTalk",
        DummyConfig("openai"),
    )

    assert "キャンセル" in result
    assert seen["tool_name"] == "web_search"
    assert seen["tool_args"] == {"query": "AoiTalk"}
    assert "OpenAI API" in seen["description"]
