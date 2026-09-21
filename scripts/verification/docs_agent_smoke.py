"""Exercise Docs Agent protocols with real PostgreSQL, BGE-M3 and local Qdrant.

Requires an explicitly disposable local database. Never accepts the application
database. All canonical rows created here are synthetic and removed on exit.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def run():
    if (os.environ.get("POSTGRES_HOST") != "127.0.0.1" or os.environ.get("POSTGRES_PORT") != "55432"
            or not os.environ.get("POSTGRES_DB", "").startswith("aoitalk_test_")):
        raise RuntimeError("Explicit local disposable PostgreSQL target required")
    UUID(os.environ["AOITALK_VERIFICATION_RUN_ID"])
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import torch
    from qdrant_client import QdrantClient
    from sqlalchemy import delete
    from src.memory.database import DatabaseManager
    from src.memory.models import DocsLibrary, KnowledgeNode, User
    from src.memory.models.docs_agent import DocsIndexQueue
    from src.rag import docs_index
    from src.rag.config import EmbeddingConfig, RagConfig
    from src.services.docs_consistency import issue_edit_lease, revision
    from src.services.docs_coverage_service import DocsCoverageService
    from src.services.docs_graph_service import DocsGraphService
    from src.services.docs_index_worker import DocsIndexWorker
    from src.services.docs_mutation_service import DocsMutationService
    from src.services.docs_read_projection import build_docs_read_projection
    from src.services.docs_scope import DocsScopeMode, resolve_docs_scope

    torch.set_num_threads(4)
    started = time.perf_counter()
    database = DatabaseManager()
    index = docs_index.DocsIndexService(RagConfig(embedding=EmbeddingConfig(device="cpu")))
    actor_id, library_id = uuid4(), uuid4()
    docs_index._durable_worker_active = True
    session = None
    try:
        assert await database.initialize(max_retries=1)
        session = await database.get_session()
        session.add(User(id=actor_id, username="docs_smoke_" + actor_id.hex, password_hash="synthetic-fixture", role="user", is_active=True))
        # The real schema enforces DocsLibrary.owner_user_id -> users.id.
        # Flush the principal before adding the dependent library so this
        # fixture also exercises the migrated FK contract.
        await session.flush()
        session.add(DocsLibrary(id=library_id, owner_user_id=actor_id, name="Synthetic Docs smoke", library_type="personal"))
        await session.flush()
        graph = DocsGraphService(session)
        root = await graph.create_node(docs_library_id=library_id, user_id=actor_id, title="Protocol verification")
        child = await graph.create_node(docs_library_id=library_id, user_id=actor_id, parent=root, title="Receipt guarantees",
            body_json={"format": "doc_block", "block_type": "markdown", "content": "操作IDを記録する。"})
        root_id, child_id = root.id, child.id
        await session.commit()
        before = await revision(session, library_id)
        projection = await build_docs_read_projection(graph, root, actor_id, include_manifest=True)
        lease = await issue_edit_lease(session, root=root, actor_id=actor_id, before_revision=before,
                                      projection=projection, binding="smoke")
        lease_id = lease.id
        await session.commit()
        operation_id = uuid4()
        changes = {"intent": "revise_section", "operations": [
            {"op": "update", "node_id": str(child_id), "content": "同じ操作IDの再送では二重作成を防止する。\n結果は保存済みreceiptから返す。"},
        ]}
        service = DocsMutationService(session)
        result = await service.apply(actor_id=actor_id, root_id=root_id, write_token=lease_id,
            operation_id=operation_id, changeset=changes, binding="smoke", before_commit=lambda: None)
        await session.commit()
        replay = await service.apply(actor_id=actor_id, root_id=root_id, write_token=lease_id,
            operation_id=operation_id, changeset=changes, binding="smoke", before_commit=lambda: None)
        assert replay["replayed"] and replay["revision"] == result["revision"]
        await session.commit()

        assert await index.embedding.initialize()
        index.client = QdrantClient(location=":memory:")
        index._is_local_mode = True
        index._ensure_collection()
        index._initialized = True
        worker = DocsIndexWorker(database.get_session, index=index)
        for _ in range(30):
            await worker.run_once()
            session.expire_all()
            pending = await session.get(DocsIndexQueue, library_id)
            ready = pending.requested_revision == pending.applied_revision
            await session.rollback()
            if ready:
                break
        assert ready, "Durable queue did not drain"
        hits = await index.search(docs_library_id=library_id, query="二重作成を防ぐ仕組み", session=session,
                                  user_id=actor_id, allowed_node_ids={child_id}, limit=3)
        assert hits.node_ids == [child_id]
        scope = await resolve_docs_scope(session=session, actor_user_id=actor_id, project_id=None, mode=DocsScopeMode.ACCESSIBLE)
        controller = DocsCoverageService(session)
        coverage = await controller.start(actor_id=actor_id, scope=scope, binding="smoke")
        coverage_id, cursor = coverage.id, coverage.state["next_cursor"]
        await session.commit()
        pages = 0
        while True:
            page = await controller.read_page(run_id=coverage_id, actor_id=actor_id, binding="smoke", cursor=cursor)
            await session.commit()
            pages += 1
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            assert pages < 100
        assert page["coverage_complete"]
        return {"success": True, "disposable": True, "driver": "asyncpg", "embedding": "BAAI/bge-m3",
                "vector_store": "Qdrant in-memory", "migration": "20260908_0005",
                "atomic_mutation": True, "receipt_replay": True, "durable_index_delivery": True,
                "authorized_semantic_search": True, "coverage_complete": True,
                "delivered_records": page["delivered_records"], "elapsed_seconds": time.perf_counter() - started}
    finally:
        if session is not None:
            await session.rollback()
            await session.execute(delete(DocsLibrary).where(DocsLibrary.id == library_id))
            await session.execute(delete(User).where(User.id == actor_id))
            await session.commit()
            await session.close()
        index.embedding.close()
        if index.client is not None:
            index.client.close()
        docs_index._durable_worker_active = False
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run())
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
