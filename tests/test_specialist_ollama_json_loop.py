from __future__ import annotations

from types import SimpleNamespace

from src.llm.json_tool_loop import JsonToolCallRecord, JsonToolLoopResult
from src.llm.specialist_delegate import (
    ProjectManagementDelegationRunner,
    SpecialistDelegationRunner,
)
from src.services.project_context import get_runtime_project_context


class FakeAgentTool:
    name = "web_search"
    description = "Search the web."
    params_json_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def __init__(self, calls):
        self.calls = calls

    def on_invoke_tool(self, ctx, payload):
        self.calls.append(payload)
        return f"searched:{payload}"


class FakeAgentClass:
    calls = []

    def __init__(self, model=None, config=None):
        self.agent = SimpleNamespace(
            instructions="Use search when needed.",
            tools=[FakeAgentTool(self.calls)],
        )


def test_specialist_ollama_runner_executes_json_tool_calls(monkeypatch):
    model_calls = []

    class FakeCompletions:
        def __init__(self):
            self._responses = [
                '{"type":"tool_call","tool":"web_search","arguments":{"query":"aquamarine mohs"}}',
                '{"type":"final","content":"Aquamarine is 7.5 to 8 on the Mohs scale."}',
            ]

        def create(self, **kwargs):
            model_calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=self._responses.pop(0))
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.base_url = base_url
            self.api_key = api_key
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.specialist_delegate.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.specialist_delegate.SpecialistDelegationRunner._configure_model_environment",
        lambda self: None,
    )

    FakeAgentClass.calls = []
    runner = SpecialistDelegationRunner(
        {
            "llm_provider": "ollama",
            "llm_model": "gemma4:e4b",
            "ollama": {"base_url": "http://localhost:11434"},
        },
        domain_key="search",
        display_name="Search",
        agent_class=FakeAgentClass,
    )

    result = runner.run("search aquamarine")

    assert result == "Aquamarine is 7.5 to 8 on the Mohs scale."
    assert runner.provider == "ollama"
    assert runner.model == "gemma4:e4b"
    assert "Tool protocol:" in model_calls[0]["messages"][0]["content"]
    assert FakeAgentClass.calls == ['{"query": "aquamarine mohs"}']


def test_project_management_ollama_result_fails_closed_without_mutation_tool():
    runner = object.__new__(ProjectManagementDelegationRunner)

    result = runner._validate_ollama_tool_loop_result(
        "add task hair appointment",
        JsonToolLoopResult(final_output="I added the task.", tool_calls=[]),
        {"create_task"},
    )

    assert "requested mutation was not completed" in result
    assert "create_task" in result
    assert "Do not tell the user" in result


def test_project_management_ollama_prefers_confirmed_tool_result_over_final_text():
    runner = object.__new__(ProjectManagementDelegationRunner)

    result = runner._validate_ollama_tool_loop_result(
        "add task hair appointment",
        JsonToolLoopResult(
            final_output="プロジェクトが不明なので一般的なタスクとして記録します。",
            tool_calls=[
                JsonToolCallRecord(
                    tool="create_task",
                    arguments={"title": "hair appointment"},
                    result=(
                        '{"id":"task-1","title":"hair appointment",'
                        '"project_id":"project-123","status":"todo"}'
                    ),
                )
            ],
        ),
        {"create_task"},
    )

    assert "タスク操作を完了しました。" in result
    assert "task_id: task-1" in result
    assert "project_id: project-123" in result
    assert "プロジェクトが不明" not in result


def test_project_management_ollama_deferred_fact_requires_list_and_upsert():
    runner = object.__new__(ProjectManagementDelegationRunner)

    result = runner._validate_ollama_tool_loop_result(
        "Deferred project fact reflection after the user-facing response.",
        JsonToolLoopResult(
            final_output="Updated.",
            tool_calls=[
                JsonToolCallRecord(
                    tool="list_project_information",
                    arguments={},
                    result='{"facts":[]}',
                )
            ],
        ),
        {"list_project_information", "upsert_project_fact"},
    )

    assert "requested mutation was not completed" in result
    assert "Missing required tools: upsert_project_fact" in result


def test_project_management_formats_upsert_fact_result():
    runner = object.__new__(ProjectManagementDelegationRunner)

    result = runner._format_mutation_tool_result(
        JsonToolCallRecord(
            tool="upsert_project_fact",
            arguments={"title": "納期遅延見込み"},
            result=(
                '{"success":true,"fact":{"id":"fact-1","title":"納期遅延見込み",'
                '"project_id":"project-123","fact_type":"risk","confidence":0.7,'
                '"status":"active"}}'
            ),
        )
    )

    assert "案件情報を更新しました" in result
    assert "fact_id: fact-1" in result
    assert "fact_type: risk" in result


def test_specialist_runner_sets_runtime_project_context_for_tools(monkeypatch):
    seen_contexts = []

    class ContextAgentTool:
        name = "create_task"
        description = "Create a task."
        params_json_schema = {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        }

        def on_invoke_tool(self, ctx, payload):
            seen_contexts.append(get_runtime_project_context())
            return "created"

    class ContextAgentClass:
        def __init__(self, model=None, config=None):
            self.agent = SimpleNamespace(
                instructions="Use create_task for task creation.",
                tools=[ContextAgentTool()],
            )

    class FakeCompletions:
        def __init__(self):
            self._responses = [
                '{"type":"tool_call","tool":"create_task","arguments":{"title":"hair appointment"}}',
                '{"type":"final","content":"Created."}',
            ]

        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=self._responses.pop(0))
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.specialist_delegate.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.specialist_delegate.SpecialistDelegationRunner._configure_model_environment",
        lambda self: None,
    )

    runner = SpecialistDelegationRunner(
        {
            "llm_provider": "ollama",
            "llm_model": "gemma4:e4b",
            "ollama": {"base_url": "http://localhost:11434"},
        },
        domain_key="project_management",
        display_name="ProjectManagement",
        agent_class=ContextAgentClass,
    )

    result = runner.run(
        "add task hair appointment",
        project_context={"id": "project-123", "name": "Selected Project"},
    )

    assert result == "Created."
    assert seen_contexts == [{"id": "project-123", "name": "Selected Project"}]
    assert get_runtime_project_context() is None
