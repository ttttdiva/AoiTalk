from __future__ import annotations

from src.llm.cli_llm_client import CLILLMClient
from src.llm.gemini_engine import GeminiLLMClient
from src.services.project_context import (
    build_project_context,
    reset_runtime_project_context,
    set_runtime_project_context,
)
from src.services.scenario_chat_context import ScenarioChatContext
from src.tools.registry import ToolRegistry


def _scenario_context() -> ScenarioChatContext:
    return ScenarioChatContext(
        mode="writing",
        agent_name="ScenarioWritingAssistant",
        allowed_tools=frozenset({"writing_assistant"}),
        prompt="SCENARIO ONLY SYSTEM PROMPT",
    )


def test_gemini_scenario_context_replaces_header_assistant_prompt():
    client = object.__new__(GeminiLLMClient)
    client.conversation_history = []
    client._get_scenario_chat_context_sync = _scenario_context
    client._build_system_prompt = lambda: "HEADER ASSISTANT PROMPT"

    token = set_runtime_project_context(
        build_project_context(
            {
                "id": "project-1",
                "name": "Header Project",
                "slug": "header-project",
            }
        )
    )
    try:
        messages = client._build_conversation_context("続きを書いて")
    finally:
        reset_runtime_project_context(token)

    system_prompt = messages[0]["parts"][0]
    assert "SCENARIO ONLY SYSTEM PROMPT" in system_prompt
    assert "HEADER ASSISTANT PROMPT" not in system_prompt
    assert "Header Project" not in system_prompt


def test_cli_scenario_context_replaces_custom_and_header_prompt():
    client = object.__new__(CLILLMClient)
    client.custom_system_prompt = "CUSTOM HEADER PROMPT"
    client.character_name = "header-agent"
    client.config = None
    client.session_user_id = "user-1"
    client.history_manager = type(
        "History",
        (),
        {"get_context_as_text": lambda self: ""},
    )()
    client._tool_registry = ToolRegistry()
    client._get_scenario_chat_context_sync = _scenario_context

    context = client._build_system_context()

    assert "SCENARIO ONLY SYSTEM PROMPT" in context
    assert "CUSTOM HEADER PROMPT" not in context
    assert "header-agent" not in context
    assert "dedicated scenario workflow instructions" in context
