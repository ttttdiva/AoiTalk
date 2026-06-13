from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.llm.generation_policy import GenerationProfile, generation_policy_for_profile
from src.llm.manager import AgentLLMClient


def _client(profile: GenerationProfile | None) -> AgentLLMClient:
    client = AgentLLMClient.__new__(AgentLLMClient)
    client.generation_policy = generation_policy_for_profile(profile)
    client.config = {}
    client._runner_kwargs = lambda: {}
    return client


class FakeToolRegistry:
    def __init__(self, available: set[str]):
        self.available = available
        self.calls: list[tuple[str, str]] = []

    def __contains__(self, name: str) -> bool:
        return name in self.available

    async def execute_async(self, name: str, **kwargs):
        self.calls.append((name, kwargs["request"]))
        return f"{name} result: task_id=task-123"


@pytest.mark.asyncio
async def test_default_chat_profile_skips_agentic_review(monkeypatch):
    calls: list[str] = []

    async def fake_run(agent, context, **kwargs):
        calls.append(context)
        return SimpleNamespace(final_output="quick answer")

    monkeypatch.setattr("src.llm.manager.Runner.run", fake_run)

    response = await AgentLLMClient._run_agentic_completion_loop(
        _client(None),
        object(),
        "answer directly",
        None,
    )

    assert response == "quick answer"
    assert calls == ["answer directly"]


@pytest.mark.asyncio
async def test_chat_profile_skips_agentic_review(monkeypatch):
    calls: list[str] = []

    async def fake_run(agent, context, **kwargs):
        calls.append(context)
        return SimpleNamespace(final_output="chat answer")

    monkeypatch.setattr("src.llm.manager.Runner.run", fake_run)

    response = await AgentLLMClient._run_agentic_completion_loop(
        _client(GenerationProfile.CHAT),
        object(),
        "answer as chat only",
        None,
    )

    assert response == "chat answer"
    assert calls == ["answer as chat only"]


@pytest.mark.asyncio
async def test_agentic_mode_continues_after_failed_review(monkeypatch):
    calls: list[str] = []
    outputs = [
        "created schedule.xlsx",
        '{"status":"continue","reason":"workbook was not verified","next_request":"open the workbook and fix missing rows"}',
        "created schedule.xlsx and verified rows",
        '{"status":"done","reason":"workbook matches the request"}',
    ]

    async def fake_run(agent, context, **kwargs):
        calls.append(context)
        return SimpleNamespace(final_output=outputs.pop(0))

    monkeypatch.setattr("src.llm.manager.Runner.run", fake_run)

    response = await AgentLLMClient._run_agentic_completion_loop(
        _client(GenerationProfile.AUTONOMOUS_WORK),
        object(),
        "create a schedule Excel file",
        None,
    )

    assert response == "created schedule.xlsx and verified rows"
    assert len(calls) == 4
    assert "completion verifier" in calls[1]
    assert "Required continuation" in calls[2]


@pytest.mark.asyncio
async def test_required_delegation_runs_project_management_for_task_requests():
    client = _client(GenerationProfile.CHAT)
    registry = FakeToolRegistry({"project_management_assistant"})
    client._tool_registry = registry

    context = await AgentLLMClient._build_required_delegation_context(
        client,
        "タスク追加して\n```\n2026年05月23日（土）14:00 サンプル店舗 カット\n```",
    )

    assert len(registry.calls) == 1
    tool_name, request = registry.calls[0]
    assert tool_name == "project_management_assistant"
    assert "Use the built-in project management tools" in request
    assert "Do not report" in request
    assert "Required Project Management Delegation Result" in context
    assert "task_id=task-123" in context


@pytest.mark.asyncio
async def test_required_delegation_runs_project_management_for_project_db_requests():
    client = _client(GenerationProfile.CHAT)
    registry = FakeToolRegistry({"project_management_assistant"})
    client._tool_registry = registry

    context = await AgentLLMClient._build_required_delegation_context(
        client,
        "ExampleCorp Firewallの案件情報DBを完成させて",
    )

    assert len(registry.calls) == 1
    tool_name, request = registry.calls[0]
    assert tool_name == "project_management_assistant"
    assert "record-table, or project-information mutation" in request
    assert "issue-table sync" in request
    assert "Required Project Management Delegation Result" in context


@pytest.mark.asyncio
async def test_required_delegation_runs_utility_for_time_requests():
    client = _client(GenerationProfile.CHAT)
    registry = FakeToolRegistry({"utility_assistant"})
    client._tool_registry = registry

    context = await AgentLLMClient._build_required_delegation_context(
        client,
        "\u4eca\u306f\u4f55\u6642\uff1f",
    )

    assert len(registry.calls) == 1
    tool_name, request = registry.calls[0]
    assert tool_name == "utility_assistant"
    assert "Use the utility specialist tools" in request
    assert "Required Utility Delegation Result" in context


@pytest.mark.asyncio
async def test_required_delegation_skips_plain_chat_requests():
    client = _client(GenerationProfile.CHAT)
    registry = FakeToolRegistry({"project_management_assistant"})
    client._tool_registry = registry

    context = await AgentLLMClient._build_required_delegation_context(
        client,
        "今日は暑いね",
    )

    assert context == ""
    assert registry.calls == []
