"""Read-only dry-run inventory for the 0015 Project Docs migration.

The report lists the exact node IDs/titles/workspace/parent/project values that
the migration would consider for duplicate archiving.  It never calls Alembic
or executes write statements; the transaction is explicitly marked READ ONLY.
Use this before applying 0015 on a database that may contain legacy Personal
Docs or Agent Memory rows.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool


ROOT = Path(__file__).resolve().parents[1]


def _load_audit_sql() -> str:
    path = ROOT / "alembic" / "versions" / "20260808_0015_migrate_project_docs.py"
    spec = importlib.util.spec_from_file_location("aoi_project_docs_migration", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load migration source: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._PROJECT_DOCS_AUDIT_SQL)


async def collect_audit(session: AsyncSession) -> dict[str, Any]:
    """Collect candidate details without mutating database state."""

    await session.execute(text("SET TRANSACTION READ ONLY"))
    rows = (
        await session.execute(text(_load_audit_sql()))
    ).mappings().all()
    target_count = len(rows)
    report = {
        "schema_version": 1,
        "mode": "read_only_dry_run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database_revision": await session.scalar(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        ),
        # ``rows`` is the expanded, cycle-safe archive subtree, not merely
        # the number of candidate roots.  Keep the historical key as an alias
        # while making the count's meaning explicit for operators.
        "archive_target_count": target_count,
        "candidate_count": target_count,
        "candidates": [dict(row) for row in rows],
    }
    await session.rollback()
    return report


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.env_file:
        load_dotenv(Path(args.env_file), override=True)
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    database = os.getenv("POSTGRES_DB", "aoitalk_memory")
    user = os.getenv("POSTGRES_USER", "aoitalk")
    password = os.getenv("POSTGRES_PASSWORD", "")
    url = (
        f"postgresql+asyncpg://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{database}"
    )
    # Do not use DatabaseManager here; startup may run Alembic migrations.
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await collect_audit(session)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="Optional dotenv file (never printed)")
    parser.add_argument("--format", choices=("json", "jsonl"), default="json")
    args = parser.parse_args()
    report = asyncio.run(_run(args))
    if args.format == "jsonl":
        for candidate in report["candidates"]:
            print(json.dumps(candidate, ensure_ascii=False, default=str))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
