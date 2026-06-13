from __future__ import annotations

from src.llm.json_tool_loop import (
    build_json_tool_loop_system_prompt,
    parse_json_tool_action,
    run_json_tool_loop,
)
from src.tools.core import tool
from src.tools.registry import ToolRegistry


def _registry() -> tuple[ToolRegistry, list[str]]:
    calls: list[str] = []
    registry = ToolRegistry()

    @tool
    def web_search(query: str) -> str:
        """Search the web."""
        calls.append(query)
        return f"searched:{query}"

    registry.register(web_search)
    return registry, calls


def test_json_tool_loop_executes_tool_and_returns_final_answer():
    registry, calls = _registry()
    completions = [
        '{"type":"tool_call","tool":"web_search","arguments":{"query":"aquamarine mohs"}}',
        '{"type":"final","content":"Aquamarine is 7.5 to 8 on the Mohs scale."}',
    ]
    seen_messages = []

    def create_completion(messages):
        seen_messages.append(list(messages))
        return completions.pop(0)

    result = run_json_tool_loop(
        create_completion=create_completion,
        initial_messages=[{"role": "user", "content": "check it"}],
        registry=registry,
    )

    assert result == "Aquamarine is 7.5 to 8 on the Mohs scale."
    assert calls == ["aquamarine mohs"]
    assert "searched:aquamarine mohs" in seen_messages[1][-1]["content"]


def test_json_tool_loop_accepts_fenced_json_and_string_arguments():
    registry, calls = _registry()
    completions = [
        '```json\n{"type":"tool_call","tool":"web_search","arguments":"{\\"query\\":\\"beryl hardness\\"}"}\n```',
        '{"type":"final","content":"done"}',
    ]

    result = run_json_tool_loop(
        create_completion=lambda messages: completions.pop(0),
        initial_messages=[{"role": "user", "content": "check it"}],
        registry=registry,
    )

    assert result == "done"
    assert calls == ["beryl hardness"]


def test_json_tool_loop_repairs_garbled_request_argument():
    registry, calls = _registry()
    completions = [
        '{"type":"tool_call","tool":"web_search","arguments":{"query":"aquamarine"}}',
        '{"type":"tool_call","tool":"request_tool","arguments":{"request":"????????"}}',
        '{"type":"final","content":"done"}',
    ]
    request_calls = []

    @tool
    def request_tool(request: str) -> str:
        """Accept a delegated request."""
        request_calls.append(request)
        return "ok"

    registry.register(request_tool)

    result = run_json_tool_loop(
        create_completion=lambda messages: completions.pop(0),
        initial_messages=[{"role": "user", "content": "check it"}],
        registry=registry,
        original_request="念のため検索して",
    )

    assert result == "done"
    assert calls == ["aquamarine"]
    assert request_calls == ["念のため検索して"]


def test_json_tool_loop_repairs_empty_request_response_before_final():
    registry = ToolRegistry()
    request_calls = []

    @tool
    def search_assistant(request: str) -> str:
        """Search through the specialist."""
        request_calls.append(request)
        return "search result"

    registry.register(search_assistant)
    completions = [
        '{"type":"final","content":"The user request is empty. Please provide a request."}',
        '{"type":"tool_call","tool":"search_assistant","arguments":{"request":"????????"}}',
        '{"type":"final","content":"done"}',
    ]

    result = run_json_tool_loop(
        create_completion=lambda messages: completions.pop(0),
        initial_messages=[{"role": "user", "content": "wrapped request"}],
        registry=registry,
        original_request="念のため検索して",
    )

    assert result == "done"
    assert request_calls == ["念のため検索して"]


def test_json_tool_loop_retries_success_claim_without_required_tool():
    registry = ToolRegistry()
    task_calls = []

    @tool
    def create_task(title: str) -> str:
        """Create a task."""
        task_calls.append(title)
        return "created task id=task-1"

    registry.register(create_task)
    completions = [
        '{"type":"final","content":"I added the task."}',
        '{"type":"tool_call","tool":"create_task","arguments":{"title":"hair appointment"}}',
        '{"type":"final","content":"Created task task-1."}',
    ]

    result = run_json_tool_loop(
        create_completion=lambda messages: completions.pop(0),
        initial_messages=[{"role": "user", "content": "add task"}],
        registry=registry,
        original_request="add task hair appointment",
        required_tool_names={"create_task"},
    )

    assert result == "Created task task-1."
    assert task_calls == ["hair appointment"]


def test_json_tool_prompt_lists_available_tools():
    registry, _ = _registry()

    prompt = build_json_tool_loop_system_prompt("base", registry)

    assert "Tool protocol:" in prompt
    assert '"name": "web_search"' in prompt


def test_parse_json_tool_action_returns_none_for_plain_text():
    assert parse_json_tool_action("plain answer") is None
