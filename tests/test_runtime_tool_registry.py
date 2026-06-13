from __future__ import annotations

import asyncio
import importlib

from src.llm.runtime_tool_registry import build_runtime_tool_registry
from src.llm.tool_policy import reset_current_user_input, set_current_user_input


def _base_config() -> dict:
    return {
        "use_tools": True,
        "mcp_enabled": True,
        "memory": {"enabled": True, "enable_search": True},
        "spotify": {"enabled": True},
        "skills": {"enabled": True},
        "agents": {
            "search": {"enabled": True},
            "project_management": {"enabled": True},
            "spotify": {"enabled": True},
            "filesystem": {"enabled": True},
            "utility": {"enabled": True},
            "media": {"enabled": True},
        },
    }


def _fake_runner(name: str, calls: list[tuple[str, str]]):
    class FakeRunner:
        def __init__(self, config):
            self.config = config

        def run(self, request: str, project_context=None) -> str:
            calls.append((name, request))
            return f"{name}:{request}"

        async def run_async(self, request: str, project_context=None) -> str:
            calls.append((name, request))
            return f"{name}:{request}"

    return FakeRunner


def test_runtime_tool_registry_exposes_search_plus_specialists(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.SpotifyDelegationRunner",
        _fake_runner("spotify", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.FilesystemDelegationRunner",
        _fake_runner("filesystem", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.UtilityDelegationRunner",
        _fake_runner("utility", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.MediaDelegationRunner",
        _fake_runner("media", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.ProjectManagementDelegationRunner",
        _fake_runner("project_management", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.ScenarioDelegationRunner",
        _fake_runner("scenario", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.WritingDelegationRunner",
        _fake_runner("writing", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.ImportDelegationRunner",
        _fake_runner("import", calls),
    )
    registry = build_runtime_tool_registry(_base_config())

    names = registry.get_names()
    assert "search_assistant" in names
    assert "web_search" not in names
    assert "grok_x_search" not in names
    assert "search_memory" not in names
    assert "knowledge_search" not in names
    assert "create_skill" not in names
    assert "create_trpg_scenario" not in names
    assert "spotify_assistant" in names
    assert "filesystem_assistant" in names
    assert "utility_assistant" in names
    assert "media_assistant" in names
    assert "project_management_assistant" in names
    assert "scenario_assistant" in names
    assert "writing_assistant" in names
    assert "import_assistant" in names
    assert "invoke_skill" in names
    assert "skills_assistant" not in names
    
    assert "use_mcp_tool" not in registry
    assert "generate_image" not in registry


def test_runtime_tool_registry_respects_specialist_feature_flags(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.FilesystemDelegationRunner",
        _fake_runner("filesystem", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.UtilityDelegationRunner",
        _fake_runner("utility", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.MediaDelegationRunner",
        _fake_runner("media", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.ProjectManagementDelegationRunner",
        _fake_runner("project_management", calls),
    )

    config = _base_config()
    config["spotify"]["enabled"] = False
    config["skills"]["enabled"] = False

    registry = build_runtime_tool_registry(config)

    assert "spotify_assistant" not in registry
    assert "invoke_skill" not in registry
    assert "project_management_assistant" in registry
    assert "filesystem_assistant" in registry


def test_advanced_reasoning_assistant_exposed_when_model_sharing_enabled(monkeypatch):
    config = _base_config()
    config["llm_provider"] = "openai"
    config["model_sharing"] = {"enabled": True}
    config["skills"]["enabled"] = False
    config["spotify"]["enabled"] = False
    for value in config["agents"].values():
        value["enabled"] = False

    registry = build_runtime_tool_registry(config)
    assert "advanced_reasoning_assistant" in registry
    assert "model_" + "handoff" not in registry
    tool = registry.get("advanced_reasoning_assistant")
    assert tool is not None
    params = {param.name: param for param in tool.parameters}
    assert params["request"].required
    assert not params["redacted_request"].required

    config["model_sharing"] = {"enabled": False}
    registry = build_runtime_tool_registry(config)
    assert "advanced_reasoning_assistant" not in registry


def test_runtime_tool_registry_executes_specialist_delegation(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.SpotifyDelegationRunner",
        _fake_runner("spotify", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.FilesystemDelegationRunner",
        _fake_runner("filesystem", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.UtilityDelegationRunner",
        _fake_runner("utility", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.MediaDelegationRunner",
        _fake_runner("media", calls),
    )
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.ProjectManagementDelegationRunner",
        _fake_runner("project_management", calls),
    )

    registry = build_runtime_tool_registry(_base_config())

    result = registry.execute(
        "spotify_assistant",
        request="Queue Hikaru Utada in Spotify",
    )

    assert result == "spotify:Queue Hikaru Utada in Spotify"
    assert calls == [("spotify", "Queue Hikaru Utada in Spotify")]


def test_runtime_tool_registry_awaits_specialist_on_current_event_loop(monkeypatch):
    calls: list[tuple[str, str]] = []
    loop_ids: list[int] = []

    class AsyncOnlyRunner:
        def __init__(self, config):
            self.config = config

        def run(self, request: str, project_context=None) -> str:
            raise AssertionError("async registry execution must not use run()")

        async def run_async(self, request: str, project_context=None) -> str:
            loop_ids.append(id(asyncio.get_running_loop()))
            calls.append(("filesystem", request))
            return f"filesystem:{request}"

    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.FilesystemDelegationRunner",
        AsyncOnlyRunner,
    )
    registry = build_runtime_tool_registry(_base_config())

    async def execute():
        expected_loop_id = id(asyncio.get_running_loop())
        result = await registry.execute_async(
            "filesystem_assistant",
            request="docs/ の一覧を確認して",
        )
        return expected_loop_id, result

    expected_loop_id, result = asyncio.run(execute())

    assert result == "filesystem:docs/ の一覧を確認して"
    assert calls == [("filesystem", "docs/ の一覧を確認して")]
    assert loop_ids == [expected_loop_id]


def test_search_assistant_uses_direct_local_search_when_only_local_web_is_enabled(monkeypatch):
    seen = {}

    def _fake_local_search(query, config=None):
        seen["query"] = query
        seen["config"] = config
        return "local-search-result"

    web_search_module = importlib.import_module("src.tools.basic.web_search")
    monkeypatch.setattr(web_search_module, "web_search_with_config", _fake_local_search)

    config = _base_config()
    config["search"] = {
        "provider": "local",
        "x_enabled": False,
        "grok_x_enabled": False,
        "knowledge_enabled": False,
    }
    config["memory"]["enable_search"] = False

    registry = build_runtime_tool_registry(config)

    result = registry.execute(
        "search_assistant",
        request="\u30a2\u30af\u30a2\u30de\u30ea\u30f3\u306e\u30e2\u30fc\u30b9\u786c\u5ea6\u3092\u8abf\u3079\u3066\u304f\u3060\u3055\u3044\u3002",
    )

    assert result == "local-search-result"
    assert seen == {
        "query": "\u30a2\u30af\u30a2\u30de\u30ea\u30f3\u306e\u30e2\u30fc\u30b9\u786c\u5ea6",
        "config": config,
    }


def test_runtime_tool_registry_omits_memory_search_when_disabled():
    config = _base_config()
    config["memory"]["enable_search"] = False

    registry = build_runtime_tool_registry(config)

    assert "search_memory" not in registry
    assert "search_assistant" in registry


def test_runtime_tool_policy_blocks_filesystem_for_general_public_query(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.FilesystemDelegationRunner",
        _fake_runner("filesystem", calls),
    )

    registry = build_runtime_tool_registry(_base_config())
    token = set_current_user_input("銃検査の時持ってく書類を箇条書きにして端的にまとめて")
    try:
        result = registry.execute(
            "filesystem_assistant",
            request="現在のプロジェクトのファイルから銃検査の書類を探してください。",
        )
    finally:
        reset_current_user_input(token)

    assert "Tool policy blocked `filesystem_assistant`" in result
    assert calls == []


def test_runtime_tool_policy_allows_filesystem_for_explicit_file_query(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.FilesystemDelegationRunner",
        _fake_runner("filesystem", calls),
    )

    registry = build_runtime_tool_registry(_base_config())
    token = set_current_user_input("docs/ のファイルを読んで要点をまとめて")
    try:
        result = registry.execute(
            "filesystem_assistant",
            request="docs/ のファイルを読んで要点をまとめて",
        )
    finally:
        reset_current_user_input(token)

    assert result == "filesystem:docs/ のファイルを読んで要点をまとめて"
    assert calls == [("filesystem", "docs/ のファイルを読んで要点をまとめて")]


def test_runtime_tool_policy_allows_japanese_folder_read_request(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.FilesystemDelegationRunner",
        _fake_runner("filesystem", calls),
    )

    registry = build_runtime_tool_registry(_base_config())
    request = (
        "ExampleCorp Firewall導入プロジェクトのフォルダにある"
        "「AIエージェント共有_20260514」が読めるか、"
        "下層構造を確認して。"
    )
    token = set_current_user_input(request)
    try:
        result = registry.execute("filesystem_assistant", request=request)
    finally:
        reset_current_user_input(token)

    assert result == f"filesystem:{request}"
    assert calls == [("filesystem", request)]


def test_runtime_tool_policy_blocks_project_management_for_general_public_query(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.ProjectManagementDelegationRunner",
        _fake_runner("project_management", calls),
    )

    registry = build_runtime_tool_registry(_base_config())
    token = set_current_user_input("銃検査の時持ってく書類を箇条書きにして端的にまとめて")
    try:
        result = registry.execute(
            "project_management_assistant",
            request="案件情報から銃検査の持参書類を確認してください。",
        )
    finally:
        reset_current_user_input(token)

    assert "Tool policy blocked `project_management_assistant`" in result
    assert calls == []


def test_runtime_tool_policy_blocks_project_management_for_simple_fact_query(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.ProjectManagementDelegationRunner",
        _fake_runner("project_management", calls),
    )

    registry = build_runtime_tool_registry(_base_config())
    token = set_current_user_input("アクアマリンのモース硬度は？")
    try:
        result = registry.execute(
            "project_management_assistant",
            request="現在の案件コンテキストからアクアマリンのモース硬度を確認してください。",
        )
    finally:
        reset_current_user_input(token)

    assert "Tool policy blocked `project_management_assistant`" in result
    assert calls == []


def test_runtime_tool_policy_allows_project_management_for_task_query(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.llm.runtime_tool_registry.ProjectManagementDelegationRunner",
        _fake_runner("project_management", calls),
    )

    registry = build_runtime_tool_registry(_base_config())
    token = set_current_user_input("この案件にタスクを作成して")
    try:
        result = registry.execute(
            "project_management_assistant",
            request="この案件にタスクを作成して",
        )
    finally:
        reset_current_user_input(token)

    assert result == "project_management:この案件にタスクを作成して"
    assert calls == [("project_management", "この案件にタスクを作成して")]
