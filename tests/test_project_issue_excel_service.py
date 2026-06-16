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


def _write_demo_issue_book(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "課題ToDo管理表"
    sheet.append(
        [
            "#",
            "タイトル",
            "起票日",
            "起票者",
            "分類",
            "詳細",
            "対応者",
            "期限",
            "回答・対応\n（更新は赤字追記)",
            "備考",
            "完了日",
            "表示/非表示",
        ]
    )
    sheet.append(
        [
            1,
            "試験について",
            "2026-06-09",
            "八巻",
            "QA",
            "結合・障害試験のみでよいか",
            "本間",
            None,
            "問題ありません",
            None,
            "2026-06-09",
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


def test_read_issue_rows_prefers_issue_management_name_over_newer_generic_file(
    tmp_path: Path,
    monkeypatch,
):
    project_id = uuid4()
    storage_root = tmp_path / "workspaces" / "_projects" / f"project_{project_id}"
    issue_file = storage_root / "01.課題管理表" / "課題ToDo管理表.xlsx"
    generic_file = storage_root / "00.スケジュール" / "最終課題研修スケジュール.xlsx"
    _write_issue_book(issue_file, ["進行中"])
    _write_issue_book(generic_file, ["完了", "完了"])
    os.utime(issue_file, (1_700_000_000, 1_700_000_000))
    os.utime(generic_file, (1_800_000_000, 1_800_000_000))
    monkeypatch.setenv("AOITALK_PROJECT_ROOT", str(tmp_path))

    rows, errors, source_file = read_issue_rows(
        {
            "id": str(project_id),
            "issue_file": "01.課題管理表/課題ToDo管理表.xlsx",
        }
    )

    assert errors == []
    assert source_file == "01.課題管理表/課題ToDo管理表.xlsx"
    assert [row.status for row in rows] == ["進行中"]


def test_read_issue_rows_supports_todo_sheet_without_status_column(
    tmp_path: Path,
    monkeypatch,
):
    project_id = uuid4()
    storage_root = tmp_path / "workspaces" / "_projects" / f"project_{project_id}"
    issue_file = storage_root / "01.課題管理表" / "課題ToDo管理表.xlsx"
    _write_demo_issue_book(issue_file)
    monkeypatch.setenv("AOITALK_PROJECT_ROOT", str(tmp_path))

    rows, errors, source_file = read_issue_rows(
        {
            "id": str(project_id),
            "issue_file": "01.課題管理表/課題ToDo管理表.xlsx",
        }
    )

    assert errors == []
    assert source_file == "01.課題管理表/課題ToDo管理表.xlsx"
    assert len(rows) == 1
    assert rows[0].title == "試験について"
    assert rows[0].kind == "QA"
    assert rows[0].owner == "本間"
    assert rows[0].history == "問題ありません"
    assert rows[0].resolved_at == "2026-06-09"
    assert rows[0].status == "完了"
