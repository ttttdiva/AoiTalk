"""Measure real search ACL code against synthetic rows in a disposable database.

Only localhost:55432/aoitalk_test_* is accepted. Owns one random schema, with
no canonical files or user records. Separates SQL candidate ACL from vector,
encoding, snippet and original acquisition times. Tables preserve ORM types;
unrelated foreign keys and services are deliberately outside this fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import uuid

import numpy as np
from sqlalchemy import Column, Index, MetaData, Table, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def stats(values, statement_counts):
    return {
        "observations": len(values),
        "p50_seconds": float(np.percentile(values, 50)),
        "p95_seconds": float(np.percentile(values, 95)),
        "sql_statements_min": min(statement_counts),
        "sql_statements_max": max(statement_counts),
    }


async def run(args):
    from src.memory.models import (
        DocsLibrary, GeneratedMedia, KnowledgeNode, KnowledgeNodeShare,
        KnowledgeNodeSupertag, KnowledgeSupertag, Project, ProjectMember, User, Space,
    )
    from src.services.docs_graph_service import DocsGraphService
    from src.services.docs_acl import apply_docs_search_visibility
    from src.services.search_sources import SearchScope, SearchSourceResolver, SourceRef, SourceUnavailable

    url = make_url(args.database_url)
    if not (url.host == "127.0.0.1" and url.port == 55432 and url.database.startswith("aoitalk_test_")):
        raise ValueError("Use only the isolated localhost:55432 aoitalk_test_* database")
    schema = "eg2_acl_fixture_" + uuid.uuid4().hex
    # Match DatabaseManager: PostgreSQL JIT is disabled in the application.
    # A large ACL expression otherwise measures repeated JIT compilation,
    # rather than the query plan used by real search requests.
    engine = create_async_engine(
        url, echo=False, connect_args={"server_settings": {"jit": "off"}},
    )
    scoped = engine.execution_options(schema_translate_map={None: schema})
    metadata = MetaData()
    tables = {}
    for model in (
        DocsLibrary, GeneratedMedia, KnowledgeNode, KnowledgeNodeShare,
        KnowledgeNodeSupertag, KnowledgeSupertag, Project, ProjectMember, User, Space,
    ):
        tables[model] = Table(model.__tablename__, metadata, *[
            Column(c.name, c.type, primary_key=c.primary_key)
            for c in model.__table__.columns
        ])
    # Candidate-related indexes, without constraints/other application tables.
    for name in ("docs_library_id", "project_id", "parent_id"):
        Index("fixture_node_" + name, tables[KnowledgeNode].c[name])
    Index("fixture_member_project_user", tables[ProjectMember].c.project_id, tables[ProjectMember].c.user_id)
    actor, other, library, readable_project, private_project = [uuid.uuid4() for _ in range(5)]
    source_count = args.sources
    refs = [SourceRef("generated_media", str(uuid.uuid5(uuid.NAMESPACE_URL, f"eg2-acl-source:{i}"))) for i in range(source_count)]
    expected_sources = sum(i % 2 for i in range(source_count))
    report = {
        "status": "FAIL",
        "synthetic_only": True,
        "backend": "PostgreSQL 16; real Docs candidate relation / SearchSourceResolver",
        "scope": "synthetic Personal/generated-media and Project-membership rows with current Project/Space SQL (Projects have no Space); excludes model encoding, vector search, result/original retrieval",
        "docs_rows": args.docs,
        "media_sources": source_count,
        "media_allowed_sources": expected_sources,
        "python_ranks_or_fetches_vectors": False,
    }
    if args.phase == "bulk" and args.output.exists():
        report.update(json.loads(args.output.read_text(encoding="utf-8")))
        report["status"] = "FAIL"
    created = False
    statement_count = 0

    def count_statement(*_):
        nonlocal statement_count
        statement_count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        async with engine.begin() as db:
            await db.execute(CreateSchema(schema))
        created = True
        async with scoped.begin() as db:
            await db.run_sync(metadata.create_all)
            await db.execute(tables[User].insert(), [
                {"id": actor, "username": "fixture_actor", "role": "user", "is_active": True},
                {"id": other, "username": "fixture_other", "role": "user", "is_active": True},
            ])
            await db.execute(tables[DocsLibrary].insert(), {"id": library, "name": "fixture", "owner_user_id": actor, "library_type": "personal"})
            await db.execute(tables[Project].insert(), [
                {"id": readable_project, "name": "fixture_readable", "slug": "fixture_readable", "owner_id": other, "deleted_at": None},
                {"id": private_project, "name": "fixture_private", "slug": "fixture_private", "owner_id": other, "deleted_at": None},
            ])
            await db.execute(tables[ProjectMember].insert(), {
                "id": uuid.uuid4(), "project_id": readable_project, "user_id": actor, "permissions": {"read": True},
            })
            for offset in range(0, args.docs, 1000):
                await db.execute(tables[KnowledgeNode].insert(), [{
                    "id": uuid.uuid5(uuid.NAMESPACE_URL, f"eg2-acl-node:{i}"),
                    "docs_library_id": library, "title": "Synthetic Japanese 日本語 RTX-830",
                    "project_id": None if i % 3 == 0 else (readable_project if i % 3 == 1 else private_project),
                    "archived_at": None, "system_key": None, "node_type": "node", "is_explicit_blank": False,
                } for i in range(offset, min(args.docs, offset + 1000))])
            for offset in range(0, source_count, 1000):
                await db.execute(tables[GeneratedMedia].insert(), [{
                    "id": uuid.UUID(refs[i].id), "owner_user_id": str(actor if i % 2 else other),
                    "context_type": "fixture", "context_id": "fixture", "status": "succeeded",
                    "relative_path": f"fixture_{i}.png", "storage_key": f"fixture_{i}",
                    "mime_type": "image/png", "prompt_meta": {},
                } for i in range(offset, min(source_count, offset + 1000))])
            for table in (tables[KnowledgeNode], tables[GeneratedMedia]):
                from sqlalchemy import text
                await db.execute(text(f'ANALYZE "{schema}"."{table.name}"'))
        async with AsyncSession(scoped) as session:
            graph = DocsGraphService(session)
            resolver = SearchSourceResolver(session, SearchScope(actor))
            if args.phase in ("docs", "baseline", "all"):
                values, counts = [], []
                for _ in range(args.observations):
                    statement_count = 0
                    started = time.perf_counter()
                    relation, _library = await graph._build_structured_query_statement(
                        docs_library_id=library, user_id=actor,
                    )
                    relation = apply_docs_search_visibility(relation, user_id=actor)
                    eligibility = relation.with_only_columns(KnowledgeNode.id, maintain_column_froms=True)
                    ids = set((await session.execute(eligibility)).scalars().all())
                    values.append(time.perf_counter() - started)
                    counts.append(statement_count)
                    assert len(ids) == sum(i % 3 != 2 for i in range(args.docs))
                report["docs_candidate_sql"] = {**stats(values, counts), "allowed_node_count": len(ids)}
                print(json.dumps({"case": "docs_candidate_sql", **report["docs_candidate_sql"]}), flush=True)
            if args.phase in ("baseline", "all"):
                values, counts = [], []
                for _ in range(3):
                    statement_count = 0
                    started = time.perf_counter()
                    allowed = []
                    # The replaced per-source loop, retained only for evidence.
                    for ref in refs:
                        try:
                            await resolver.resolve(ref, open_file=False)
                            allowed.append(ref.key)
                        except SourceUnavailable:
                            pass
                    values.append(time.perf_counter() - started)
                    counts.append(statement_count)
                    assert len(allowed) == expected_sources
                    print(json.dumps({"case": "media_legacy_per_source_acl", "seconds": values[-1], "sql_statements": counts[-1]}), flush=True)
                report["media_legacy_per_source_acl"] = stats(values, counts)
            if args.phase in ("bulk", "all"):
                values, counts = [], []
                for _ in range(args.observations):
                    statement_count = 0
                    started = time.perf_counter()
                    allowed = await resolver.allowed_refs(refs)
                    values.append(time.perf_counter() - started)
                    counts.append(statement_count)
                    assert len(allowed) == expected_sources
                report["media_bulk_candidate_acl"] = stats(values, counts)
                print(json.dumps({"case": "media_bulk_candidate_acl", **report["media_bulk_candidate_acl"]}), flush=True)
            report["status"] = "PASS_LOCAL_SQL_FIXTURE" if args.phase != "baseline" else "PASS_BASELINE_PENDING_BULK"
            return report
    finally:
        if created:
            async with engine.begin() as db:
                await db.execute(DropSchema(schema, cascade=True))
        await engine.dispose()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--docs", type=int, default=145922)
    parser.add_argument("--sources", type=int, default=14593)
    parser.add_argument("--observations", type=int, default=30)
    parser.add_argument("--phase", choices=["docs", "baseline", "bulk", "all"], default="all")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.docs < 3 or args.sources < 2 or args.observations < 3:
        parser.error("Need at least 3 Docs, 2 sources and 3 observations")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
