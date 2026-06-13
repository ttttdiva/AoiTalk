from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.services.scenario_chat_context import (
    build_scenario_chat_context,
    is_scenario_workflow_tool_allowed,
    is_scenario_workflow_tool_disabled,
)


@pytest.mark.asyncio
async def test_writing_session_context_disables_project_management(monkeypatch):
    from src.services import scenario_service
    from src.tools import writing_tools

    async def fake_writing_session(conversation_id: str):
        return {
            "id": "write-1",
            "conversation_session_id": conversation_id,
            "scenario_id": "scenario-1",
            "target_scene_id": "scene-1",
        }

    monkeypatch.setattr(
        scenario_service,
        "get_writing_session_by_conversation",
        fake_writing_session,
    )
    monkeypatch.setattr(
        scenario_service,
        "get_play_session_by_conversation_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        writing_tools.get_writing_context,
        "execute_async",
        AsyncMock(return_value="## 現在のシーン\n本文コンテキスト"),
    )

    context = await build_scenario_chat_context("conv-123")

    assert context is not None
    assert context.mode == "writing"
    assert context.agent_name == "ScenarioWritingAssistant"
    assert "Scenario Workflow: Writing" in context.prompt
    assert "conversation_id`: conv-123" in context.prompt
    assert is_scenario_workflow_tool_allowed("writing_assistant", context)
    assert is_scenario_workflow_tool_allowed("import_assistant", context)
    assert not is_scenario_workflow_tool_allowed(
        "project_management_assistant",
        context,
    )
    assert is_scenario_workflow_tool_disabled("project_management_assistant")


@pytest.mark.asyncio
async def test_roleplay_context_uses_scenario_character(monkeypatch):
    from src.services import scenario_service

    monkeypatch.setattr(
        scenario_service,
        "get_writing_session_by_conversation",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        scenario_service,
        "get_play_session_by_conversation_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        scenario_service,
        "get_scenario",
        AsyncMock(
            return_value={
                "id": "scenario-1",
                "title": "夜の駅",
                "description": "駅で出会う話",
                "characters": [
                    {
                        "id": "char-1",
                        "name": "琴葉葵",
                        "role": "npc",
                        "description": "静かな案内人",
                    }
                ],
            }
        ),
    )

    class FakeConversationRepository:
        async def get_session_by_id(self, session_id: str):
            return SimpleNamespace(
                character_name="scenario_roleplay:scenario-1:char-1"
            )

    monkeypatch.setattr(
        "src.memory.conversation_repository.ConversationRepository",
        FakeConversationRepository,
    )

    context = await build_scenario_chat_context("conv-456")

    assert context is not None
    assert context.mode == "roleplay"
    assert context.agent_name == "琴葉葵"
    assert "Scenario Workflow: Scenario Roleplay" in context.prompt
    assert "琴葉葵" in context.prompt
    assert "app-header assistant/character selection is not part" in context.prompt
    assert is_scenario_workflow_tool_allowed("scenario_assistant", context)
    assert not is_scenario_workflow_tool_allowed("writing_assistant", context)
