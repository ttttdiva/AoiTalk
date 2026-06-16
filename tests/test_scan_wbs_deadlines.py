from datetime import date
import importlib.util
from pathlib import Path

from openpyxl import Workbook

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "scan_wbs_deadlines.py"
SPEC = importlib.util.spec_from_file_location("scan_wbs_deadlines", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
scan_wbs_deadlines = MODULE.scan_wbs_deadlines


def test_scan_wbs_deadlines_reads_tasks_after_first_twenty_rows(tmp_path: Path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "WBS"
    sheet.append(["WBS番号", "タスク名", "予定終了日", "進捗率", "担当"])
    for index in range(1, 25):
        sheet.append([f"1.{index}", f"将来タスク{index}", "2026-12-31", "0%", "担当A"])
    sheet.append(["1.25", "期限接近タスク", "2026-06-15", "50%", "担当B"])
    path = tmp_path / "WBS.xlsx"
    workbook.save(path)
    workbook.close()

    result = scan_wbs_deadlines(
        path,
        threshold_days=2,
        reference_date=date(2026, 6, 14),
    )

    assert result["scanned_task_count"] == 25
    assert result["matched_task_count"] == 1
    assert result["tasks"][0]["title"] == "期限接近タスク"
    assert result["tasks"][0]["row_number"] == 26
