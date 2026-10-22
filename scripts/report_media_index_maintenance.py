"""Read-only bookkeeping report and purge plan. No default paths to runtime data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.services.media_index_maintenance import maintenance_report, purge_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, help="Explicit canonical/count metadata snapshot JSON")
    parser.add_argument("--retention-days", type=float, default=30)
    parser.add_argument("--plan-out", type=Path,
                        help="Write a pinned plan (max 100 candidates) for apply_media_index_purge.py")
    args = parser.parse_args(argv)
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else None
        report = maintenance_report(ledger=args.ledger, queue=args.queue, evidence=evidence,
                                    retention_days=args.retention_days)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"report unavailable: {exc}\n")
    if args.plan_out:
        args.plan_out.write_text(json.dumps(purge_plan(report), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
