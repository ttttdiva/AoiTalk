from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.llm.gemini_engine import GeminiLLMClient
from src.services.context_builder import ContextBuilder, ContextBundle
from src.services.project_context import build_project_context, get_runtime_project_context


class _DummyConfig:
    default_character = "test"

    def __init__(self) -> None:
        self._data = {
            "memory": {"enabled": False},
            "mcp_enabled": False,
            "use_tools": True,
            "spotify": {"enabled": False},
            "skills": {"enabled": True},
            "agents": {
                "project_management": {"enabled": True},
                "search": {"enabled": True},
                "spotify": {"enabled": False},
                "filesystem": {"enabled": True},
                "utility": {"enabled": True},
                "media": {"enabled": True},
            },
        }

    def get(self, key, default=None):
        return self._data.get(key, default)

    def get_character_config(self, character_name: str):
        return {
            "name": character_name,
            "personality": {"details": "Test persona."},
        }


class _FakeToolRegistry:
    def __init__(self, available: set[str]):
        self.available = available
        self.calls: list[tuple[str, str]] = []

    def __contains__(self, name: str) -> bool:
        return name in self.available

    def execute(self, name: str, **kwargs):
        self.calls.append((name, kwargs["request"]))
        return f"{name} result: task_id=task-456"


def test_gemini_client_uses_runtime_registry_and_unified_prompt(monkeypatch):
    monkeypatch.setattr("src.llm.gemini_engine.genai.configure", lambda **_: None)
    monkeypatch.setattr(
        "src.llm.gemini_engine.genai.GenerativeModel",
        lambda *args, **kwargs: SimpleNamespace(args=args, kwargs=kwargs),
    )
    monkeypatch.setattr(
        "src.tools.entertainment.spotify_tools.init_spotify_manager",
        lambda: False,
    )

    client = GeminiLLMClient(
        api_key="test-key",
        model="gemini-test",
        config=_DummyConfig(),
    )

    tool_names = client._tool_registry.get_names()

    assert "project_management_assistant" in tool_names
    assert "filesystem_assistant" in tool_names
    assert "search_assistant" in tool_names
    assert "invoke_skill" in tool_names
    assert "use_mcp_tool" not in tool_names
    assert "project_management_assistant" in client.system_prompt
    assert "メインassistantからMCPツールを直接呼ばない" in client.system_prompt


def test_gemini_client_sets_runtime_project_context_during_tool_calls(monkeypatch):
    class _FakeChat:
        def __init__(self) -> None:
            self._call_count = 0

        def send_message(self, *_args, **_kwargs):
            self._call_count += 1
            if self._call_count == 1:
                return SimpleNamespace(
                    candidates=[
                        SimpleNamespace(
                            content=SimpleNamespace(
                                parts=[
                                    SimpleNamespace(
                                        function_call=SimpleNamespace(
                                            name="project_management_assistant",
                                            args={"request": "test03ってタスク作って"},
                                        ),
                                        text=None,
                                    )
                                ]
                            )
                        )
                    ]
                )
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[SimpleNamespace(function_call=None, text="done")]
                        )
                    )
                ]
            )

    class _FakeModel:
        def start_chat(self, history=None):
            return _FakeChat()

    project_context = build_project_context(
        {
            "id": "0ebe7ef0-7559-4a34-8114-ad6a4d336e60",
            "name": "Inbox",
            "slug": "inbox",
            "description": "Default project",
        }
    )

    async def _resolve_context(self, project_id=None, session_id=None):
        return project_context

    monkeypatch.setattr("src.llm.gemini_engine.genai.configure", lambda **_: None)
    monkeypatch.setattr(
        "src.llm.gemini_engine.genai.GenerativeModel",
        lambda *args, **kwargs: _FakeModel(),
    )
    monkeypatch.setattr(
        "src.llm.gemini_engine.ProjectContextResolver.resolve_context",
        _resolve_context,
    )
    monkeypatch.setattr(
        "src.tools.entertainment.spotify_tools.init_spotify_manager",
        lambda: False,
    )

    client = GeminiLLMClient(
        api_key="test-key",
        model="gemini-test",
        config=_DummyConfig(),
    )
    client.current_project_id = project_context["id"]

    seen = {}

    def _fake_execute_tool(function_name, arguments):
        seen["tool_name"] = function_name
        seen["arguments"] = arguments
        seen["project_context"] = get_runtime_project_context()
        return "tool-result"

    client._execute_tool = _fake_execute_tool

    result = client.generate_response("test03ってタスク作って")

    assert result == "done"
    assert seen["tool_name"] == "project_management_assistant"
    assert seen["arguments"] == {"request": "test03ってタスク作って"}
    assert seen["project_context"]["id"] == project_context["id"]
    assert get_runtime_project_context() is None


def test_gemini_keeps_runtime_project_context_when_prompt_context_disabled(
    monkeypatch,
):
    project_context = build_project_context(
        {
            "id": "0ebe7ef0-7559-4a34-8114-ad6a4d336e60",
            "name": "Inbox",
            "slug": "inbox",
            "description": "Default project",
        }
    )

    async def _resolve_context(self, project_id=None, session_id=None):
        return project_context

    captured = {}

    async def _build_context(self, **kwargs):
        captured.update(kwargs)
        return ContextBundle(memory_context_block="user only")

    monkeypatch.setattr(
        "src.llm.gemini_engine.ProjectContextResolver.resolve_context",
        _resolve_context,
    )
    monkeypatch.setattr(ContextBuilder, "build_context", _build_context)

    client = GeminiLLMClient.__new__(GeminiLLMClient)
    client.current_project_id = project_context["id"]
    client.current_session_id = None
    client.current_include_project_context = False
    client._get_scenario_chat_context_sync = lambda: None
    client._get_session_user_id = lambda: "default_user"
    client._run_async_sync = lambda coro: asyncio.run(coro)

    resolved = GeminiLLMClient._resolve_project_context_sync(client)
    bundle = GeminiLLMClient._build_context_bundle_sync(client, "hello", resolved)

    assert resolved["id"] == project_context["id"]
    assert captured["project_context"]["id"] == project_context["id"]
    assert captured["include_project_context"] is False
    assert bundle.render_for_prompt() == "user only"


def test_gemini_required_delegation_runs_project_management_for_task_requests():
    client = GeminiLLMClient.__new__(GeminiLLMClient)
    registry = _FakeToolRegistry({"project_management_assistant"})
    client._tool_registry = registry

    context = GeminiLLMClient._build_required_delegation_context(
        client,
        "タスク追加して\n```\n2026年05月23日（土）14:00 サンプル店舗 カット\n```",
    )

    assert len(registry.calls) == 1
    tool_name, request = registry.calls[0]
    assert tool_name == "project_management_assistant"
    assert "Use the built-in project management tools" in request
    assert "never ask for project" in request
    assert "Required Project Management Delegation Result" in context
    assert "task_id=task-456" in context


def test_gemini_required_delegation_runs_utility_for_time_requests():
    client = GeminiLLMClient.__new__(GeminiLLMClient)
    registry = _FakeToolRegistry({"utility_assistant"})
    client._tool_registry = registry

    context = GeminiLLMClient._build_required_delegation_context(
        client,
        "\u4eca\u306f\u4f55\u6642\uff1f",
    )

    assert len(registry.calls) == 1
    tool_name, request = registry.calls[0]
    assert tool_name == "utility_assistant"
    assert "Use the utility specialist tools" in request
    assert "Required Utility Delegation Result" in context


def test_gemini_required_delegation_skips_plain_chat_requests():
    client = GeminiLLMClient.__new__(GeminiLLMClient)
    registry = _FakeToolRegistry({"project_management_assistant"})
    client._tool_registry = registry

    context = GeminiLLMClient._build_required_delegation_context(
        client,
        "今日は暑いね",
    )

    assert context == ""
    assert registry.calls == []
