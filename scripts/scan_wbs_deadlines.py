"""Scan every WBS task row and emit upcoming or overdue deadlines as JSON."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.wbs_excel_service import read_wbs_file

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def scan_wbs_deadlines(
    workbook_path: Path,
    *,
    threshold_days: int = 14,
    reference_date: date | None = None,
) -> dict[str, Any]:
    if threshold_days < 0:
        raise ValueError("threshold_days must be zero or greater.")

    path = workbook_path.expanduser().resolve()
    rows, errors = read_wbs_file(path, relative_file_path=str(path))
    if errors:
        raise ValueError("; ".join(errors))

    today = reference_date or date.today()
    matches: list[dict[str, Any]] = []
    for row in rows:
        if row.status == "closed" or not row.planned_end:
            continue
        due_date = date.fromisoformat(row.planned_end)
        days_remaining = (due_date - today).days
        if days_remaining > threshold_days:
            continue
        matches.append(
            {
                "wbs_id": row.wbs_id,
                "title": row.title,
                "planned_end": row.planned_end,
                "days_remaining": days_remaining,
                "overdue": days_remaining < 0,
                "progress": row.progress,
                "status": row.status,
                "assignee": row.assignee,
                "sheet_name": row.sheet_name,
                "row_number": row.row_number,
            }
        )

    matches.sort(
        key=lambda item: (
            item["planned_end"],
            item["sheet_name"],
            item["row_number"],
        )
    )
    return {
        "path": str(path),
        "reference_date": today.isoformat(),
        "threshold_days": threshold_days,
        "scanned_task_count": len(rows),
        "matched_task_count": len(matches),
        "tasks": matches,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan all WBS rows for upcoming and overdue planned end dates."
    )
    parser.add_argument("workbook", type=Path, help=".xlsx/.xlsm WBS file")
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Include unfinished tasks due within this many days, plus all overdue tasks.",
    )
    parser.add_argument(
        "--reference-date",
        type=date.fromisoformat,
        help="Reference date in YYYY-MM-DD format. Defaults to today.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = scan_wbs_deadlines(
            args.workbook,
            threshold_days=args.days,
            reference_date=args.reference_date,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps({"success": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
