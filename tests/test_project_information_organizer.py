from pathlib import Path
from uuid import uuid4

import pytest

from src.services.project_information_organizer import (
    heuristic_organize,
    normalize_project_folder_path,
    resolve_project_folder,
    scan_project_folder,
)


def test_normalize_project_folder_path_accepts_project_filer_prefix():
    project_id = uuid4()

    assert (
        normalize_project_folder_path(
            project_id,
            f"_projects/project_{project_id}/6.お客様受領資料/10_M.プロジェクト管理",
        )
        == "6.お客様受領資料/10_M.プロジェクト管理"
    )
    assert normalize_project_folder_path(project_id, f"_projects/project_{project_id}") == ""
    assert normalize_project_folder_path(project_id, "../bad/./safe") == "bad/safe"


def test_resolve_project_folder_blocks_outside_paths(tmp_path: Path):
    project_id = uuid4()
    (tmp_path / "docs").mkdir()

    target, relative = resolve_project_folder(tmp_path, project_id, "docs")
    assert target == (tmp_path / "docs").resolve()
    assert relative == "docs"

    with pytest.raises(FileNotFoundError):
        resolve_project_folder(tmp_path, project_id, "../../outside")


def test_scan_project_folder_extracts_supported_files(tmp_path: Path):
    project_id = uuid4()
    folder = tmp_path / "案件資料"
    folder.mkdir()
    (folder / "概要.md").write_text("# 概要\nこの案件はFirewall更新です。", encoding="utf-8")
    (folder / "ignore.exe").write_bytes(b"bad")

    files, relative = scan_project_folder(tmp_path, project_id, "案件資料")

    assert relative == "案件資料"
    assert [file.name for file in files] == ["概要.md"]
    assert "Firewall" in files[0].extracted_text


def test_heuristic_organize_creates_documents_and_facts():
    from src.services.project_information_organizer import ScannedProjectFile

    files = [
        ScannedProjectFile(
            path="資料/parameter.xlsx",
            name="parameter.xlsx",
            extension=".xlsx",
            size_bytes=10,
            modified_at="2026-05-14T00:00:00",
            extracted_text="パラメータシート\n決定: NAT方針は既存踏襲\n要確認: 切替日時",
        )
    ]

    draft = heuristic_organize("ExampleCorp Firewall", "資料", files)

    assert draft.documents[0].document_type == "parameter_sheet"
    assert draft.documents[0].category_key == "detail_design"
    assert any(fact.category_key == "decisions" for fact in draft.facts)
    assert any(fact.category_key == "open_questions" for fact in draft.facts)


def test_heuristic_organize_adds_firewall_project_specific_categories():
    from src.services.project_information_organizer import ScannedProjectFile

    files = [
        ScannedProjectFile(
            path="office/design.docx",
            name="design.docx",
            extension=".docx",
            size_bytes=10,
            modified_at="2026-05-14T00:00:00",
            extracted_text=(
                "既存構成では基幹SWから建屋SWへ接続している。"
                "FirewallとしてPA-560を導入し、Port-channel/AEを確認する。"
            ),
        )
    ]

    draft = heuristic_organize("ExampleCorp Firewall", "office", files)
    category_keys = {category.key for category in draft.categories}

    assert "existing_configuration" in category_keys
    assert "edge_firewall" in category_keys
    assert "building_switches" in category_keys
    assert "control_core_switch" in category_keys
    assert not [fact for fact in draft.facts if fact.fact_type == "document_summary"]


def test_heuristic_organize_skips_reference_sample_marker_noise():
    from src.services.project_information_organizer import ScannedProjectFile

    files = [
        ScannedProjectFile(
            path="03_一次資料/テスト参考/sample.xlsx",
            name="sample.xlsx",
            extension=".xlsx",
            size_bytes=10,
            modified_at="2026-05-14T00:00:00",
            extracted_text="承認者: サンプル\n| NaN | 要確認 | NaN |",
        )
    ]

    draft = heuristic_organize("ExampleCorp Firewall", "03_一次資料/テスト参考", files)

    assert not [fact for fact in draft.facts if fact.fact_type == "decision"]
    assert not [fact for fact in draft.facts if fact.fact_type == "open_question"]


def test_heuristic_organize_does_not_turn_markdown_table_rows_into_facts():
    from src.services.project_information_organizer import ScannedProjectFile

    files = [
        ScannedProjectFile(
            path="docs/接続構成.md",
            name="接続構成.md",
            extension=".md",
            size_bytes=10,
            modified_at="2026-05-14T00:00:00",
            extracted_text=(
                "| 6 | PA⇔5トリム南棟建屋SW | PA-560 Active/Passive | "
                "Building-A-SW01 | 光 | 4 | 要確認 |"
            ),
        )
    ]

    draft = heuristic_organize("ExampleCorp Firewall", "docs", files)

    assert all("|" not in fact.content for fact in draft.facts)
