from __future__ import annotations

from types import SimpleNamespace

from src.llm.ollama_engine import OllamaClient
from src.tools.core import tool
from src.tools.registry import ToolRegistry


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


def test_ollama_uses_json_tool_loop_when_native_tool_calling_is_disabled(monkeypatch):
    calls = []
    registry = ToolRegistry()

    @tool
    def search_assistant(request: str) -> str:
        """Delegate search work."""
        return f"search-result:{request}"

    registry.register(search_assistant)

    class FakeCompletions:
        def __init__(self):
            self._responses = [
                '{"type":"tool_call","tool":"search_assistant","arguments":{"request":"aquamarine mohs hardness"}}',
                '{"type":"final","content":"Aquamarine is 7.5 to 8 on the Mohs scale."}',
            ]

        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=self._responses.pop(0),
                            tool_calls=None,
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.ollama_engine.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.ollama_engine.build_runtime_tool_registry",
        lambda config: registry,
    )
    monkeypatch.setattr(
        "src.llm.ollama_engine.build_unified_instructions",
        lambda **kwargs: "system prompt",
    )
    monkeypatch.setattr(
        "src.llm.ollama_engine.get_user_custom_instructions_sync",
        lambda user_id: "",
    )

    config = DummyConfig(
        {
            "default_character": "zundamon",
            "llm_model": "gemma4:e4b",
            "use_tools": True,
            "ollama": {"enable_tools": False},
        }
    )
    client = OllamaClient(config=config)

    response = client.generate_response("search aquamarine")

    assert response == "Aquamarine is 7.5 to 8 on the Mohs scale."
    assert len(calls) == 2
    assert "tools" not in calls[0]
    assert "Tool protocol:" in calls[0]["messages"][0]["content"]
    assert "search-result:aquamarine mohs hardness" in calls[1]["messages"][-1]["content"]


def test_ollama_injects_required_project_delegation_before_parent_answer(monkeypatch):
    calls = []
    delegated_requests = []
    registry = ToolRegistry()

    @tool
    def project_management_assistant(request: str) -> str:
        """Delegate project management work."""
        delegated_requests.append(request)
        return "Created task id=task-1 title=hair appointment"

    registry.register(project_management_assistant)

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="parent answer with task-1",
                            tool_calls=None,
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.ollama_engine.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.ollama_engine.build_runtime_tool_registry",
        lambda config: registry,
    )
    monkeypatch.setattr(
        "src.llm.ollama_engine.build_unified_instructions",
        lambda **kwargs: "system prompt",
    )
    monkeypatch.setattr(
        "src.llm.ollama_engine.get_user_custom_instructions_sync",
        lambda user_id: "",
    )

    config = DummyConfig(
        {
            "default_character": "zundamon",
            "llm_model": "gemma4:e4b",
            "use_tools": True,
            "ollama": {"enable_tools": False},
        }
    )
    client = OllamaClient(config=config)

    response = client.generate_response("add task hair appointment tomorrow")

    assert response == "parent answer with task-1"
    assert len(delegated_requests) == 1
    assert "Use the built-in project management tools" in delegated_requests[0]
    assert len(calls) == 1
    parent_user_message = calls[0]["messages"][-1]["content"]
    assert "Required Project Management Delegation Result" in parent_user_message
    assert "task-1" in parent_user_message
    assert "Current user request:" in parent_user_message
