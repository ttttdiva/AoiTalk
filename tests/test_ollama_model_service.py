from src.services.ollama_model_service import (
    OllamaModelManager,
    get_ollama_base_url,
    normalize_ollama_base_url,
)


class DummyConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        value = self.values
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


def test_normalize_ollama_base_url_strips_openai_suffix():
    assert (
        normalize_ollama_base_url("http://127.0.0.1:11434/v1")
        == "http://127.0.0.1:11434"
    )


def test_get_ollama_base_url_uses_nested_config(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    config = DummyConfig({"ollama": {"base_url": "http://localhost:11434/v1"}})

    assert get_ollama_base_url(config) == "http://localhost:11434"


def test_list_models_uses_ollama_tags_endpoint(monkeypatch):
    urls = []

    def fake_get(url, timeout):
        urls.append((url, timeout))
        if url.endswith("/api/tags"):
            return FakeResponse(
                {
                    "models": [
                        {"name": "z-model:latest", "size": 2},
                        {"name": "a-model:latest", "size": 1},
                    ]
                }
            )
        return FakeResponse({"version": "0.23.1"})

    monkeypatch.setattr("src.services.ollama_model_service.requests.get", fake_get)
    manager = OllamaModelManager(
        DummyConfig({"ollama": {"base_url": "http://127.0.0.1:11434/v1"}})
    )

    result = manager.list_models()

    assert result["success"] is True
    assert urls[0] == ("http://127.0.0.1:11434/api/tags", 1.0)
    assert [model["name"] for model in result["models"]] == [
        "a-model:latest",
        "z-model:latest",
    ]


def test_delete_model_uses_ollama_delete_endpoint(monkeypatch):
    calls = []

    def fake_delete(url, json, timeout):
        calls.append((url, json, timeout))
        return FakeResponse({})

    monkeypatch.setattr("src.services.ollama_model_service.requests.delete", fake_delete)
    manager = OllamaModelManager(
        DummyConfig({"ollama": {"base_url": "http://127.0.0.1:11434/v1"}})
    )

    result = manager.delete_model("gemma4:e4b")

    assert result == {
        "success": True,
        "base_url": "http://127.0.0.1:11434",
        "model": "gemma4:e4b",
    }
    assert calls == [
        (
            "http://127.0.0.1:11434/api/delete",
            {"model": "gemma4:e4b"},
            10.0,
        )
    ]


def test_delete_model_requires_model_name():
    manager = OllamaModelManager(DummyConfig({}))

    try:
        manager.delete_model("  ")
    except ValueError as exc:
        assert str(exc) == "model is required"
    else:
        raise AssertionError("delete_model should reject empty model names")
