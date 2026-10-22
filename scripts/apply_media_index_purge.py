"""Re-verify a pinned purge plan; deletes only with --execute.

Run only during a maintenance window after a fresh report/plan (max 1 hour).
Each source is re-checked under the sync_source FileLock and SQLite write locks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.services.media_index_maintenance import apply_purge_plan


async def _run(args):
    factory = None
    if args.execute:
        from src.memory.database import get_database_manager

        manager = get_database_manager()

        async def factory():
            return manager.SessionLocal()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    return await apply_purge_plan(plan, ledger=args.ledger, queue=args.queue,
                                  canonical_session_factory=factory, execute=args.execute)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Delete verified rows (default: verify only)")
    args = parser.parse_args(argv)
    for path in (args.ledger, args.queue):
        if not path.is_file():
            parser.exit(2, f"database not found: {path}\n")
    try:
        result = asyncio.run(_run(args))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"purge unavailable: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
