from src.llm.generation_policy import GenerationProfile, generation_policy_for_profile
from src.llm.ollama_engine import OllamaClient


class History:
    def __init__(self):
        self.messages = []

    def add_message(self, role, content):
        self.messages.append((role, content))


def test_ollama_chat_profile_keeps_json_tool_loop(monkeypatch):
    client = object.__new__(OllamaClient)
    client.generation_policy = generation_policy_for_profile(GenerationProfile.CHAT)
    client._tool_registry = {"tool": object()}
    client._native_tool_calling_enabled = False
    client.history_manager = History()

    monkeypatch.setattr(
        client,
        "_generate_with_json_tool_loop",
        lambda *args, **kwargs: "tool reply",
    )

    assert client.generate_response("hello") == "tool reply"
    assert client.history_manager.messages == [
        ("user", "hello"),
        ("assistant", "tool reply"),
    ]


def test_ollama_autonomous_profile_keeps_json_tool_loop(monkeypatch):
    client = object.__new__(OllamaClient)
    client.generation_policy = generation_policy_for_profile(
        GenerationProfile.AUTONOMOUS_WORK
    )
    client._tool_registry = {"tool": object()}
    client._native_tool_calling_enabled = False
    client.history_manager = History()

    monkeypatch.setattr(
        client,
        "_generate_with_json_tool_loop",
        lambda *args, **kwargs: "tool reply",
    )

    assert client.generate_response("hello") == "tool reply"
