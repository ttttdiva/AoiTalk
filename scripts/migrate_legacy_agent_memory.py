"""Dry-run-first migration of legacy Agent Memory Docs to Scoped Memory.

Dry-run is the default. Database mutation requires ``--apply``. Legacy Docs
are never deleted and Project Information candidates are never promoted here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def _engine():
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    database = os.getenv("POSTGRES_DB", "aoitalk_memory")
    user = os.getenv("POSTGRES_USER", "aoitalk")
    password = os.getenv("POSTGRES_PASSWORD", "")
    url = (
        f"postgresql+asyncpg://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{database}"
    )
    return create_async_engine(url, poolclass=NullPool)


async def _session_factory(engine):
    return AsyncSession(engine, expire_on_commit=False)


async def _load_entries(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                WITH RECURSIVE roots AS (
                    SELECT node.id, node.project_id, project.owner_id
                      FROM knowledge_nodes node
                 LEFT JOIN projects project ON project.id = node.project_id
                     WHERE node.system_key LIKE 'agent_memory:%'
                ), tree AS (
                    SELECT child.id, child.parent_id, root.id AS root_id,
                           root.project_id, root.owner_id, child.title,
                           ARRAY[child.title]::text[] AS path, 1 AS depth,
                           child.archived_at
                      FROM roots root
                      JOIN knowledge_nodes child ON child.parent_id = root.id
                    UNION ALL
                    SELECT child.id, child.parent_id, tree.root_id,
                           tree.project_id, tree.owner_id, child.title,
                           tree.path || child.title, tree.depth + 1,
                           child.archived_at
                      FROM tree
                      JOIN knowledge_nodes child ON child.parent_id = tree.id
                     WHERE tree.depth < 8
                )
                SELECT tree.*,
                       revision.id AS revision_id,
                       revision.created_at AS revision_created_at
                  FROM tree
             LEFT JOIN LATERAL (
                    SELECT revision.id, revision.created_at
                      FROM knowledge_revisions revision
                     WHERE revision.node_id = tree.id
                  ORDER BY revision.created_at DESC
                     LIMIT 1
                ) revision ON TRUE
              ORDER BY tree.root_id, tree.depth, tree.id
                """
            )
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def _normalized(value: Any) -> str:
    import re

    return re.sub(
        r"[^0-9a-zぁ-んァ-ン一-龥]+",
        "",
        str(value or "").strip().casefold(),
    )


def classify_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from src.services.scoped_memory_service import classify_sensitivity

    classified: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        title = str(entry.get("title") or "").strip()
        category = "project"
        scope_type = "project"
        if entry.get("archived_at") is not None:
            category = "archived"
        elif title == "(まだ記憶はありません)" or not title:
            category = "placeholder"
        elif entry.get("project_id") is None or entry.get("owner_id") is None:
            category = "unresolved"
        else:
            sensitivity, rejection = classify_sensitivity(title)
            if rejection:
                category = "sensitive_rejected"
            elif sensitivity == "sensitive":
                category = "sensitive_candidate"
            elif any(token in title.casefold() for token in ("ユーザー", "好み", "嗜好", "prefers", "always use")):
                category = "user_candidate"
                scope_type = "user"
            elif any(token in title.casefold() for token in ("案件情報", "仕様", "要件", "project information", "constraint", "decision")):
                category = "project_information_candidate"
            elif any(token in title.casefold() for token in ("タスク", "todo", "task", "期限")):
                category = "task_candidate"
                # No trustworthy task id exists on legacy rows. Keep this as a
                # project-scoped candidate and let the user choose the target
                # task explicitly in the Memory UI.
            signature = (scope_type, str(entry.get("project_id") or entry.get("owner_id")), _normalized(title))
            if category not in {"archived", "placeholder", "unresolved", "sensitive_rejected"}:
                if signature in seen:
                    category = "duplicate"
                else:
                    seen.add(signature)
        classified.append({**entry, "category": category, "scope_type": scope_type})
    return classified


def _summary(
    classified: list[dict[str, Any]],
    *,
    migration_id: str,
    mode: str,
    failures: list[dict[str, str]] | None = None,
    mappings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    categories = Counter(str(item["category"]) for item in classified)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "migration_id": migration_id,
        "legacy_entry_count": len(classified),
        "classification_counts": dict(sorted(categories.items())),
        "mutates_legacy_docs": False,
        "auto_promotes_project_information": False,
        "failures": failures or [],
        "rollback_mapping_count": len(mappings or []),
        "rollback_mapping": mappings or [],
    }


async def _mark_legacy_docs_managed(engine, migration_id: str) -> None:
    from src.memory.models import KnowledgeNode
    from src.services.docs_graph_service import DocsGraphService
    from src.services.managed_docs_policy import (
        LEGACY_AGENT_MEMORY_DOMAIN,
        ManagedDocsPolicy,
        managed_display_props,
    )

    policy = ManagedDocsPolicy(
        managed_domain=LEGACY_AGENT_MEMORY_DOMAIN,
        allowed_tools=frozenset({"legacy_agent_memory_migration"}),
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        nodes = list(
            (
                await session.execute(
                    select(KnowledgeNode).where(
                        (KnowledgeNode.system_key == "agent_memory_root")
                        | KnowledgeNode.system_key.like("agent_memory:%")
                    )
                )
            ).scalars().all()
        )
        writer = DocsGraphService(session)
        for node in nodes:
            node.display_props = managed_display_props(
                policy,
                node.display_props if isinstance(node.display_props, dict) else None,
            )
            await writer.record_node_change(
                node,
                node.updated_by,
                "legacy Agent Memoryをread-only管理対象へ移行",
                [{"type": "scoped_memory_migration", "migration_id": migration_id}],
            )
        await session.commit()


async def _apply(engine, classified: list[dict[str, Any]], migration_id: str) -> dict[str, Any]:
    from src.services.scoped_memory_service import ScopedMemoryService

    failures: list[dict[str, str]] = []
    mappings: list[dict[str, str]] = []
    service = ScopedMemoryService(lambda: _session_factory(engine))
    skipped = {"placeholder", "archived", "unresolved", "duplicate"}
    for entry in classified:
        if entry["category"] in skipped:
            continue
        try:
            actor_id = str(entry["owner_id"])
            project_id = str(entry["project_id"])
            scope_type = str(entry["scope_type"])
            path = [str(part) for part in entry.get("path") or []]
            evidence = {
                "type": "legacy_agent_memory_doc",
                "node_id": str(entry["id"]),
                "root_id": str(entry["root_id"]),
                "path": path,
                "revision_id": str(entry["revision_id"]) if entry.get("revision_id") else None,
                "revision_created_at": (
                    entry["revision_created_at"].isoformat()
                    if entry.get("revision_created_at")
                    else None
                ),
            }
            result = await service.upsert_memory(
                actor_id=actor_id,
                content=str(entry["title"]),
                scope_type=scope_type,
                scope_id=actor_id if scope_type == "user" else project_id,
                project_id=project_id if scope_type == "project" else None,
                memory_type="legacy_agent_memory",
                structured_data={
                    "legacy_classification": entry["category"],
                    "source_path": path,
                    "suggested_scope": (
                        "task" if entry["category"] == "task_candidate" else scope_type
                    ),
                },
                source_type="legacy_migration",
                source_ref=f"knowledge_node:{entry['id']}",
                confidence=0.6,
                importance=5,
                trust_level="unverified",
                evidence_refs=[evidence],
                status=("rejected" if entry["category"] == "sensitive_rejected" else "candidate"),
                migration_id=migration_id,
                idempotency_key=f"{migration_id}:{entry['id']}",
                created_by_actor="legacy_agent_memory_migration",
            )
            mappings.append(
                {
                    "source_node_id": str(entry["id"]),
                    "memory_id": str(result["memory_id"]),
                }
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "source_node_id": str(entry.get("id") or "unknown"),
                    "error": type(exc).__name__,
                }
            )
    await _mark_legacy_docs_managed(engine, migration_id)
    return _summary(
        classified,
        migration_id=migration_id,
        mode="apply",
        failures=failures,
        mappings=mappings,
    )


async def _rollback(engine, migration_id: str, *, apply: bool) -> dict[str, Any]:
    from src.memory.models import ContextMemory
    from src.services.scoped_memory_service import ScopedMemoryService

    async with AsyncSession(engine, expire_on_commit=False) as session:
        rows = list(
            (
                await session.execute(
                    select(ContextMemory).where(ContextMemory.migration_id == migration_id)
                )
            ).scalars().all()
        )
    mappings = [
        {
            "memory_id": str(row.id),
            "source_ref": str(row.source_ref or ""),
            "status": row.status,
        }
        for row in rows
    ]
    failures: list[dict[str, str]] = []
    if apply:
        service = ScopedMemoryService(lambda: _session_factory(engine))
        for row in rows:
            if row.status in {"forgotten", "superseded"}:
                continue
            try:
                await service.forget_memory(
                    str(row.id),
                    actor_id=str(row.user_id),
                    expected_version=int(row.version or 1),
                    reason=f"rollback:{migration_id}",
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {"memory_id": str(row.id), "error": type(exc).__name__}
                )
    return {
        "schema_version": 1,
        "mode": "rollback_apply" if apply else "rollback_dry_run",
        "migration_id": migration_id,
        "matching_memory_count": len(rows),
        "legacy_docs_unchanged": True,
        "rollback_mapping": mappings,
        "failures": failures,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.env_file:
        load_dotenv(Path(args.env_file), override=True)
    engine = _engine()
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            if not args.apply:
                await session.execute(text("SET TRANSACTION READ ONLY"))
            revision = await session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
            if args.apply and revision != "20260807_0003":
                raise RuntimeError(
                    f"Scoped Memory schema is required before apply (current={revision})"
                )
            if args.rollback:
                await session.rollback()
                return await _rollback(engine, args.rollback, apply=args.apply)
            entries = await _load_entries(session)
            classified = classify_entries(entries)
            await session.rollback()
        if args.apply:
            return await _apply(engine, classified, args.migration_id)
        return _summary(
            classified,
            migration_id=args.migration_id,
            mode="dry_run",
        )
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", metavar="MIGRATION_ID")
    parser.add_argument(
        "--migration-id",
        default="legacy-agent-memory-v2-20260807",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(_run(args))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.report:
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if report.get("failures") else 0


if __name__ == "__main__":
    raise SystemExit(main())
