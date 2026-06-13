from __future__ import annotations

from types import SimpleNamespace

from src.llm.sglang_engine import SGLangClient
from src.llm.tool_policy import (
    looks_like_project_management_mutation_request,
    project_management_required_mutation_tools,
)
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

    def get_character_config(self, character_name):
        return {"name": character_name, "personality": {"details": ""}}


class FakeServerManager:
    auto_start = False
    base_url = "http://localhost:30000/v1"

    def is_running(self):
        return True


def test_sglang_injects_required_project_delegation_before_parent_answer(monkeypatch):
    calls = []
    delegated_requests = []
    registry = ToolRegistry()

    @tool
    def project_management_assistant(request: str) -> str:
        """Delegate project management work."""
        delegated_requests.append(request)
        return "タスク操作を完了しました。\n- task_id: task-1\n- project_id: project-123"

    registry.register(project_management_assistant)

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="parent answer with task_id: task-1",
                            tool_calls=None,
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, base_url, api_key):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("src.llm.sglang_engine.OpenAI", FakeOpenAI)
    monkeypatch.setattr(
        "src.llm.sglang_engine.build_runtime_tool_registry",
        lambda config: registry,
    )

    client = SGLangClient(
        base_url="http://localhost:30000/v1",
        model="qwen3",
        api_key="dummy",
        config=DummyConfig({"use_tools": True, "sglang": {"auto_start": False}}),
        server_manager=FakeServerManager(),
    )

    response = client.generate_response("予約をタスクとして追加して")

    assert response == "parent answer with task_id: task-1"
    assert len(delegated_requests) == 1
    assert "Use the built-in project management tools" in delegated_requests[0]
    assert len(calls) == 1
    parent_user_message = calls[0]["messages"][-1]["content"]
    assert "Required Project Management Delegation Result" in parent_user_message
    assert "task_id: task-1" in parent_user_message
    assert "Current user request:" in parent_user_message


def test_project_policy_detects_project_database_mutations():
    cases = {
        "ExampleCorp Firewallの案件情報DBを完成させて": {
            "upsert_project_fact",
            "register_project_document",
            "create_record_table",
            "sync_issue_table",
        },
        "この資料フォルダから案件情報を整理してDBに登録して": {
            "organize_project_information_from_folder",
        },
        "WBSからレコードテーブルを作成して": {
            "sync_wbs_tasks",
            "create_record_table",
        },
        "課題管理表をDB化して": {
            "sync_issue_table",
        },
    }

    for text, expected_tools in cases.items():
        assert looks_like_project_management_mutation_request(text)
        assert expected_tools & project_management_required_mutation_tools(text)
