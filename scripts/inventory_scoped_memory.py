"""Read-only inventory for the Scoped Memory v2 migration.

Only aggregate counts are emitted. Titles, contents, user ids, project ids,
URLs, and credentials are never included in the report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


async def _scalar(session, sql: str) -> int:
    value = await session.scalar(text(sql))
    return int(value or 0)


async def collect_inventory(session) -> dict[str, Any]:
    await session.execute(text("SET TRANSACTION READ ONLY"))

    grouped = (
        await session.execute(
            text(
                """
                SELECT COALESCE(scope_type, '(null)') AS scope_type,
                       COALESCE(source_type, '(null)') AS source_type,
                       COALESCE(status, '(null)') AS status,
                       COUNT(*) AS count
                  FROM context_memories
              GROUP BY 1, 2, 3
              ORDER BY 1, 2, 3
                """
            )
        )
    ).mappings().all()

    report: dict[str, Any] = {
        "schema_version": 1,
        "database_revision": await session.scalar(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only",
        "contains_sensitive_values": False,
        "context_memories": {
            "total": sum(int(row["count"]) for row in grouped),
            "by_scope_source_status": [dict(row) for row in grouped],
        },
        "risk_candidates": {
            "dreaming_similarity_duplicate_groups": await _scalar(
                session,
                """
                SELECT COUNT(*) FROM (
                    SELECT user_id, regexp_replace(lower(trim(content)), '[^[:alnum:]]+', '', 'g') normalized
                      FROM context_memories
                     WHERE status = 'active' AND source_type LIKE 'dreaming%'
                  GROUP BY user_id, normalized HAVING COUNT(*) > 1
                ) duplicates
                """,
            ),
            "secret_candidate_count": await _scalar(
                session,
                """
                SELECT
                  (SELECT COUNT(*) FROM context_memories WHERE content ~* '(password|passwd|secret|token|api[_ -]?key|秘密鍵)')
                  +
                  (SELECT COUNT(*) FROM knowledge_nodes WHERE title ~* '(password|passwd|secret|token|api[_ -]?key|秘密鍵)')
                """,
            ),
            "duplicated_project_information_context_count": await _scalar(
                session,
                """
                SELECT COUNT(*) FROM context_memories memory
                 WHERE memory.status = 'active' AND memory.project_id IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM projects project
                       JOIN knowledge_nodes node ON node.id = project.knowledge_node_id
                        WHERE project.id = memory.project_id
                          AND regexp_replace(lower(trim(memory.content)), '[^[:alnum:]]+', '', 'g')
                              = regexp_replace(lower(trim(node.title)), '[^[:alnum:]]+', '', 'g')
                   )
                """,
            ),
        },
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
    # Do not use DatabaseManager here: its initialization may run Alembic.
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await collect_inventory(session)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help="Optional dotenv file; its values are never printed")
    parser.add_argument("--format", choices=("json", "jsonl"), default="json")
    args = parser.parse_args()
    report = asyncio.run(_run(args))
    if args.format == "jsonl":
        for section, value in report.items():
            print(json.dumps({"section": section, "value": value}, ensure_ascii=False))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
