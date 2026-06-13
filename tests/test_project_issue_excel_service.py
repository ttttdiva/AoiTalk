import os
from pathlib import Path
from uuid import uuid4

from openpyxl import Workbook

from src.services.project_issue_excel_service import read_issue_rows, summarize_issue_rows


def _write_issue_book(path: Path, statuses: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "課題一覧"
    sheet.append([None, "■課題管理表"])
    sheet.append([])
    sheet.append(
        [
            None,
            "No",
            "区分",
            "フェーズ",
            "Status",
            "起票日",
            "起票者",
            "重要度",
            "課題概要",
            "課題詳細",
            "ActionPlan",
            "課題Close条件",
            "対応期限",
            "主担当者",
            "対応経緯",
            "対策終了日",
            "完了承認日",
            "完了承認者",
            "備考",
        ]
    )
    for index, status in enumerate(statuses, start=1):
        sheet.append(
            [
                None,
                index,
                "確認",
                "設計",
                status,
                "2026-04-10",
                "NOS",
                "中",
                f"課題{index}",
                "詳細",
                None,
                None,
                "2026-05-11",
                "SBR",
                "経緯",
                None,
                None,
                None,
                None,
            ]
        )
    workbook.save(path)


def test_read_issue_rows_prefers_newer_project_filer_issue_book(
    tmp_path: Path,
    monkeypatch,
):
    project_id = uuid4()
    storage_root = tmp_path / "workspaces" / "_projects" / f"project_{project_id}"
    old_file = storage_root / "management" / "課題管理表.xlsx"
    new_file = storage_root / "AI共有" / "02_成果物" / "課題管理表.xlsx"
    _write_issue_book(old_file, ["未着手"])
    _write_issue_book(new_file, ["完了", "進行中"])
    os.utime(old_file, (1_700_000_000, 1_700_000_000))
    os.utime(new_file, (1_800_000_000, 1_800_000_000))
    monkeypatch.setenv("AOITALK_PROJECT_ROOT", str(tmp_path))

    rows, errors, source_file = read_issue_rows(
        {
            "id": str(project_id),
            "issue_file": "management/課題管理表.xlsx",
        }
    )

    assert errors == []
    assert source_file == "AI共有/02_成果物/課題管理表.xlsx"
    assert [row.status for row in rows] == ["完了", "進行中"]
    assert summarize_issue_rows(rows)["open_count"] == 1
