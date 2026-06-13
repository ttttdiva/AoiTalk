from types import SimpleNamespace

from src.llm.ollama_engine import OllamaClient, create_ollama_client


class EmptyRegistry:
    def __len__(self):
        return 0

    def get_all(self):
        return []


class DummyConfig:
    default_character = "zundamon"

    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        value = self.values
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def _patch_common(monkeypatch, calls):
    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="OK", tool_calls=None)
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.base_url = base_url
            self.api_key = api_key
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.ollama_engine.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.ollama_engine.build_runtime_tool_registry",
        lambda config: EmptyRegistry(),
    )
    monkeypatch.setattr(
        "src.llm.ollama_engine.build_unified_instructions",
        lambda **kwargs: "system prompt",
    )
    monkeypatch.setattr(
        "src.llm.ollama_engine.get_user_custom_instructions_sync",
        lambda user_id: "",
    )


def test_generate_response_uses_openai_compatible_ollama_url(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)

    client = OllamaClient(
        base_url="http://127.0.0.1:11434",
        model="gemma4:e4b",
        api_key="ollama",
        config=None,
    )

    assert client.base_url == "http://127.0.0.1:11434/v1"
    assert client.generate_response("ping") == "OK"
    assert calls[0]["model"] == "gemma4:e4b"
    assert calls[0]["messages"][-1] == {"role": "user", "content": "ping"}
    assert client.get_history()[-1] == {"role": "assistant", "content": "OK"}


def test_gpt_oss_mode_is_sent_as_reasoning_effort(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)

    client = OllamaClient(
        base_url="http://127.0.0.1:11434",
        model="gpt-oss:20b",
        api_key="ollama",
        config=None,
    )

    client.set_llm_mode("high")

    assert client.generate_response("ping") == "OK"
    assert calls[0]["reasoning_effort"] == "high"


def test_ollama_boolean_thinking_mode_maps_to_reasoning_effort(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)

    client = OllamaClient(
        base_url="http://127.0.0.1:11434",
        model="qwen3:32b",
        api_key="ollama",
        config=None,
    )

    assert client.generate_response("fast") == "OK"
    client.set_llm_mode("thinking")
    assert client.generate_response("thinking") == "OK"

    assert calls[0]["reasoning_effort"] == "none"
    assert calls[1]["reasoning_effort"] == "medium"


def test_ollama_non_thinking_model_omits_reasoning_effort(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)

    client = OllamaClient(
        base_url="http://127.0.0.1:11434",
        model="gemma4:e4b",
        api_key="ollama",
        config=None,
    )
    client.set_llm_mode("thinking")

    assert client.generate_response("ping") == "OK"
    assert "reasoning_effort" not in calls[0]


def test_create_ollama_client_uses_config_defaults(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)
    config = DummyConfig(
        {
            "default_character": "zundamon",
            "llm_model": "gemma4:e4b",
            "ollama": {
                "base_url": "http://localhost:11434",
                "api_key": "dummy",
            },
        }
    )

    client = create_ollama_client(config)

    assert client.base_url == "http://localhost:11434/v1"
    assert client.model_name == "gemma4:e4b"
    assert client.api_key == "dummy"


def test_create_llm_client_ollama_factory_message_is_readable(monkeypatch, capsys):
    from src.llm import ollama_engine
    from src.llm.manager import create_llm_client

    sentinel = object()
    monkeypatch.setattr(ollama_engine, "create_ollama_client", lambda config: sentinel)

    client = create_llm_client(DummyConfig({"llm_provider": "ollama"}))

    assert client is sentinel
    output = capsys.readouterr().out
    assert "[LLM Factory] Ollamaクライアントを作成" in output
    assert "繧" not in output


def test_set_system_prompt_preserves_unified_prompt_when_configured(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)
    config = DummyConfig({"default_character": "zundamon"})

    client = OllamaClient(config=config)
    client.set_system_prompt("raw character prompt")

    assert client.system_prompt == "system prompt"
