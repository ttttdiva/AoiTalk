from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path


def load_bridge_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "agent_project_management_tool.py"
    spec = importlib.util.spec_from_file_location("agent_project_management_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_project_management_tool_bridge_exposes_project_information_tools():
    bridge = load_bridge_module()
    tools = bridge.build_tool_index()

    expected = {
        "list_project_information",
        "configure_project_management_files",
        "upsert_project_info_category",
        "register_project_document",
        "upsert_project_fact",
        "create_record_table",
        "append_record_rows",
        "get_project_issues",
        "sync_issue_table",
        "get_upcoming_wbs_tasks",
        "summarize_project_requests",
        "sync_wbs_tasks",
    }
    assert expected.issubset(set(tools))


def test_project_management_tool_bridge_merges_json_and_key_value_args():
    bridge = load_bridge_module()
    parsed = bridge.parse_args_json(
        '{"project":"AoiTalk","apply":false}',
        ["max_files=10", 'labels=["a","b"]', "note=plain text"],
    )

    assert parsed == {
        "project": "AoiTalk",
        "apply": False,
        "max_files": 10,
        "labels": ["a", "b"],
        "note": "plain text",
    }


def test_project_management_tool_bridge_supplies_non_null_tool_context():
    bridge = load_bridge_module()

    class FakeTool:
        name = "fake_tool"

        def on_invoke_tool(self, context, payload):
            assert context is not None
            return '{"success": true}'

    result = asyncio.run(bridge.invoke_tool(FakeTool(), {}))

    assert result == {"success": True}
