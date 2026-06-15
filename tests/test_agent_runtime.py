from __future__ import annotations

from types import SimpleNamespace

from src.llm.agent_runtime import (
    build_required_delegation_context_sync,
    compose_required_delegation_user_message,
    run_openai_tool_call_loop,
)
from src.llm.generation_policy import GenerationProfile, generation_policy_for_profile


class FakeRegistry:
    def __init__(self, available: set[str], result: str = "result") -> None:
        self.available = available
        self.result = result
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __contains__(self, name: str) -> bool:
        return name in self.available

    def execute(self, name: str, **kwargs):
        self.calls.append((name, kwargs))
        return self.result


def test_required_utility_delegation_runs_before_parent_answer():
    registry = FakeRegistry({"utility_assistant"}, "2026-06-09 12:34 JST")
    context = build_required_delegation_context_sync(
        user_input="\u4eca\u306f\u4f55\u6642\uff1f",
        registry=registry,
        policy=generation_policy_for_profile(GenerationProfile.CHAT),
    )

    assert registry.calls[0][0] == "utility_assistant"
    assert "Use the utility specialist tools" in registry.calls[0][1]["request"]
    assert "Required Utility Delegation Result" in context
    assert "2026-06-09 12:34 JST" in context


def test_required_delegation_skips_plain_chat():
    registry = FakeRegistry({"utility_assistant", "project_management_assistant"})
    context = build_required_delegation_context_sync(
        user_input="hello",
        registry=registry,
        policy=generation_policy_for_profile(GenerationProfile.CHAT),
    )

    assert context == ""
    assert registry.calls == []


def test_project_delegation_request_includes_conversation_fact_rule():
    registry = FakeRegistry({"project_management_assistant"})
    build_required_delegation_context_sync(
        user_input="この案件、機器納期が8月に遅れるらしい。WBSの期日修正して",
        registry=registry,
        policy=generation_policy_for_profile(GenerationProfile.CHAT),
    )

    request = registry.calls[0][1]["request"]
    assert "upsert_project_fact" in request
    assert "source_type='conversation'" in request
    assert "confidence below 1.0" in request


def test_compose_required_delegation_user_message_keeps_original_request():
    message = compose_required_delegation_user_message(
        "what time is it?",
        "## Required Utility Delegation Result\n2026-06-09 12:34 JST",
    )

    assert "## Required Utility Delegation Result" in message
    assert "Current user request:\nwhat time is it?" in message


def test_openai_tool_call_loop_executes_common_registry_tool():
    registry = FakeRegistry({"utility_assistant"}, "2026-06-09 12:34 JST")
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="utility_assistant",
            arguments='{"request":"current time"}',
        ),
    )
    assistant_message = SimpleNamespace(content=None, tool_calls=[tool_call])
    completions: list[dict] = []

    def create_completion(kwargs):
        completions.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="It is 2026-06-09 12:34 JST.",
                        tool_calls=None,
                    )
                )
            ]
        )

    response = run_openai_tool_call_loop(
        initial_messages=[{"role": "user", "content": "what time is it?"}],
        assistant_message=assistant_message,
        api_kwargs={"model": "local-test"},
        registry=registry,
        create_completion=create_completion,
    )

    assert response == "It is 2026-06-09 12:34 JST."
    assert registry.calls == [
        ("utility_assistant", {"request": "current time"}),
    ]
    assert completions[0]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "2026-06-09 12:34 JST",
    }
