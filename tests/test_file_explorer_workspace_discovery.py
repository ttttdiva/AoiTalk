from __future__ import annotations

from src.tools.file_explorer.file_explorer_service import (
    find_workspace_items,
    inspect_workspace_tree,
)


def test_find_workspace_items_includes_named_directories(monkeypatch, tmp_path):
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(tmp_path))
    target = tmp_path / "_projects" / "project-1" / "AIエージェント共有_20260514"
    target.mkdir(parents=True)
    (target / "README.md").write_text("read first", encoding="utf-8")

    result = find_workspace_items("AIエージェント共有", max_results=10)

    assert result["success"] is True
    assert result["total_returned"] == 1
    assert result["results"][0]["kind"] == "directory"
    assert result["results"][0]["path"].endswith("AIエージェント共有_20260514")


def test_inspect_workspace_tree_returns_bounded_files_and_folders(monkeypatch, tmp_path):
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(tmp_path))
    target = tmp_path / "_projects" / "project-1" / "handoff"
    (target / "00_最初に読む").mkdir(parents=True)
    (target / "00_最初に読む" / "AGENTS.md").write_text(
        "instructions", encoding="utf-8"
    )

    result = inspect_workspace_tree("_projects/project-1/handoff", max_depth=2)

    assert result["success"] is True
    paths = {entry["path"] for entry in result["entries"]}
    assert "_projects/project-1/handoff/00_最初に読む" in paths
    assert "_projects/project-1/handoff/00_最初に読む/AGENTS.md" in paths
    assert result["truncated"] is False
