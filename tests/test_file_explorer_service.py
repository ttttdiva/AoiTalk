from __future__ import annotations

import io
import zipfile

from src.tools.file_explorer.file_explorer_service import (
    archive_items,
    create_directory,
    download_file,
    extract_archives,
    get_full_content,
    list_directory,
    rename_item,
    save_file,
    search_files,
    upload_file,
)


def test_search_files_allows_absolute_root_for_admin(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    filer_root = tmp_path / "filer"
    filer_root.mkdir()
    (filer_root / "scene-bgm.mp3").write_text("dummy", encoding="utf-8")

    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    denied = search_files("bgm", str(filer_root), is_admin=False)
    assert denied["success"] is False

    allowed = search_files("bgm", str(filer_root), is_admin=True)
    assert allowed["success"] is True
    assert allowed["results"][0]["name"] == "scene-bgm.mp3"


def test_list_directory_returns_parent_path_for_admin_absolute_path(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    filer_root = tmp_path / "filer"
    filer_root.mkdir()
    child = filer_root / "child"
    child.mkdir()

    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    result = list_directory(str(filer_root), is_admin=True)

    assert result["success"] is True
    assert result["current_path"] == str(filer_root).replace("\\", "/")
    assert result["parent_path"] == str(tmp_path).replace("\\", "/")
    assert result["can_go_up"] is True
    assert result["is_admin_mode"] is True
    assert result["directories"][0]["path"] == str(child).replace("\\", "/")


def test_list_directory_rejects_absolute_path_for_non_admin(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    result = list_directory(str(outside), is_admin=False)

    assert result["success"] is False
    assert "無効" in result["error"]
    assert list(workspace.iterdir()) == []


def test_admin_file_operations_allow_absolute_paths(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    filer_root = tmp_path / "filer"
    filer_root.mkdir()
    target = filer_root / "note.md"

    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    denied = save_file(str(target), "blocked", is_admin=False)
    assert denied["success"] is False

    saved = save_file(str(target), "hello", is_admin=True)
    assert saved["success"] is True
    assert get_full_content(str(target), is_admin=True)["content"] == "hello"

    created = create_directory(str(filer_root), "docs", is_admin=True)
    assert created["success"] is True
    assert created["path"] == str(filer_root / "docs").replace("\\", "/")

    renamed = rename_item(str(target), "renamed.md", is_admin=True)
    assert renamed["success"] is True
    assert renamed["new_path"] == str(filer_root / "renamed.md").replace(
        "\\", "/"
    )


def test_upload_file_creates_sanitized_relative_folder_path(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    result = upload_file(
        "",
        "docs/chapter:1/note?.md",
        b"hello",
    )

    assert result["success"] is True
    assert result["name"] == "note.md"
    assert result["path"] == "docs/chapter1/note.md"
    assert (workspace / "docs" / "chapter1" / "note.md").read_bytes() == b"hello"


def test_upload_file_allows_overwrite_and_new_file_sequence(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target_dir = workspace / "docs"
    target_dir.mkdir()
    (target_dir / "existing.md").write_bytes(b"old")
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    overwritten = upload_file("docs", "existing.md", b"new")
    created = upload_file("docs", "latest.md", b"latest")

    assert overwritten["success"] is True
    assert overwritten["path"] == "docs/existing.md"
    assert created["success"] is True
    assert created["path"] == "docs/latest.md"
    assert (target_dir / "existing.md").read_bytes() == b"new"
    assert (target_dir / "latest.md").read_bytes() == b"latest"


def test_upload_file_blocks_nested_executable(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    result = upload_file("", "tools/run.bat", b"bad")

    assert result["success"] is False
    assert not (workspace / "tools").exists()


def test_download_file_returns_zip_for_folder(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    folder = workspace / "docs"
    nested = folder / "chapter"
    nested.mkdir(parents=True)
    (folder / "readme.md").write_text("hello", encoding="utf-8")
    (nested / "note.txt").write_text("nested", encoding="utf-8")
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    content, filename, mime_type = download_file("docs")

    assert filename == "docs.zip"
    assert mime_type == "application/zip"
    assert content is not None
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert sorted(archive.namelist()) == ["chapter/note.txt", "readme.md"]
        assert archive.read("readme.md") == b"hello"


def test_archive_items_creates_zip_for_selected_items(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    folder = workspace / "docs"
    nested = folder / "chapter"
    nested.mkdir(parents=True)
    (workspace / "readme.md").write_text("hello", encoding="utf-8")
    (nested / "note.txt").write_text("nested", encoding="utf-8")
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    result = archive_items(["readme.md", "docs"], "")

    assert result["success"] is True
    assert result["archive_name"] == "archive.zip"
    archive_path = workspace / "archive.zip"
    assert archive_path.exists()
    with zipfile.ZipFile(archive_path) as archive:
        assert sorted(archive.namelist()) == [
            "docs/chapter/note.txt",
            "readme.md",
        ]
        assert archive.read("docs/chapter/note.txt") == b"nested"


def test_archive_items_uses_source_name_for_single_folder(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    folder = workspace / "docs"
    folder.mkdir(parents=True)
    (folder / "readme.md").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    result = archive_items(["docs"], "")

    assert result["success"] is True
    assert result["archive_name"] == "docs.zip"
    assert (workspace / "docs.zip").exists()


def test_extract_archives_extracts_zip_to_named_folder(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    archive_path = workspace / "bundle.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("chapter/note.txt", "nested")
        archive.writestr("readme.md", "hello")
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    result = extract_archives(["bundle.zip"], "")

    assert result["success"] is True
    assert result["extracted"][0]["path"] == "bundle"
    assert (workspace / "bundle" / "readme.md").read_text(encoding="utf-8") == "hello"
    assert (
        workspace / "bundle" / "chapter" / "note.txt"
    ).read_text(encoding="utf-8") == "nested"


def test_extract_archives_rejects_zip_slip_paths(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    archive_path = workspace / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../evil.txt", "bad")
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    result = extract_archives(["bad.zip"], "")

    assert result["success"] is False
    assert not (workspace / "bad").exists()
    assert not (tmp_path / "evil.txt").exists()


def test_extract_archives_cleans_up_previous_extract_on_later_failure(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    good_archive = workspace / "good.zip"
    bad_archive = workspace / "bad.zip"
    with zipfile.ZipFile(good_archive, "w") as archive:
        archive.writestr("readme.md", "hello")
    with zipfile.ZipFile(bad_archive, "w") as archive:
        archive.writestr("../evil.txt", "bad")
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    result = extract_archives(["good.zip", "bad.zip"], "")

    assert result["success"] is False
    assert not (workspace / "good").exists()
    assert not (workspace / "bad").exists()


def test_extract_archives_rejects_non_zip_files(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("AOITALK_WORKSPACES_DIR", str(workspace))

    result = extract_archives(["note.txt"], "")

    assert result["success"] is False
    assert "ZIPファイルではありません" in result["error"]
