"""Rebuild the derived semantic index for every Docs Library.

Run this after the Docs Library rename/unification migration when the Qdrant
collection still contains payloads written by the pre-rename service::

    python scripts/reindex_docs_qdrant.py

The script is intentionally explicit and idempotent.  Postgres remains the
source of truth; ``reconcile_library`` computes hashes, upserts canonical
``docs_library_id`` payloads, and removes stale point IDs.  ``--local-path``
is useful for an offline smoke run (the production default uses configured
Qdrant host/port), while ``--device cpu`` avoids requiring CUDA on a deploy
worker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from sqlalchemy import func, select

# ``python scripts/reindex_docs_qdrant.py`` puts ``scripts/`` (rather than
# the repository root) on ``sys.path``.  Add the root explicitly so operators
# can use the documented direct command as well as ``python -m``.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.migrate_embeddinggemma2 import shadow_config
from src.memory.database import DatabaseManager
from src.memory.models import DocsLibrary
from src.rag.docs_index import DocsIndexService
from src.rag.shadow_migration import read_only_source_session


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-path",
        type=Path,
        help="Use Qdrant local storage at this path instead of the configured server.",
    )
    parser.add_argument(
        "--device",
        choices=("cpu", "cuda"),
        help="Override embedding device (default: configured device).",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict[str, int]:
    db = DatabaseManager()
    service = None
    session = None
    totals = {"libraries": 0, "nodes": 0, "reindexed": 0, "removed": 0}
    try:
        # ``get_rag_config`` constructs Config(), which may seed/fill defaults
        # when this standalone process has not initialized the DB manager yet.
        # Read the persisted global projection directly so a reindex can never
        # silently target the default Qdrant endpoint or rewrite app settings.
        async with read_only_source_session(db.SessionLocal) as source_session:
            config = await shadow_config(source_session)

        qdrant = config.qdrant
        embedding = config.embedding
        if args.local_path is not None:
            qdrant = replace(qdrant, local_path=str(args.local_path))
        if args.device is not None:
            embedding = replace(embedding, device=args.device)
        config = replace(config, qdrant=qdrant, embedding=embedding, docs_enabled=True)

        service = DocsIndexService(config)
        if not await service.initialize():
            raise RuntimeError("Docs semantic index could not initialize (Qdrant/embedding unavailable)")

        # The operator command assumes its source schema is already provisioned.
        # Use the raw factory so this read-only derived-index operation never
        # runs migrations or creates/fills app-config tables as a side effect.
        session = db.SessionLocal()
        library_ids = (
            await session.execute(
                select(DocsLibrary.id).order_by(DocsLibrary.id)
            )
        ).scalars().all()
        # Include IDs discovered only in Qdrant as well.  This is what removes
        # canonical/legacy points after the corresponding DB library/node was
        # deleted; the old ``KnowledgeNode``-only query could never visit an
        # empty library scope.
        indexed_ids = await service.indexed_library_ids()
        library_ids = sorted(set(library_ids) | indexed_ids, key=str)
        for library_id in library_ids:
            result = await service.reconcile_library(session, library_id)
            if result.get("status") != "synced":
                status = result.get("status") or "unknown"
                raise RuntimeError(
                    f"Docs semantic index reconciliation failed for {library_id}: {status}"
                )
            totals["libraries"] += 1
            totals["nodes"] += int(result.get("total", 0))
            totals["reindexed"] += int(result.get("reindexed", 0))
            totals["removed"] += int(result.get("removed", 0))
    finally:
        try:
            if session is not None:
                await session.close()
        finally:
            try:
                await db.close()
            finally:
                if service is not None:
                    client = getattr(service, "client", None)
                    try:
                        close_client = getattr(client, "close", None)
                        if callable(close_client):
                            close_client()
                    finally:
                        embedding = getattr(service, "embedding", None)
                        close_embedding = getattr(embedding, "close", None)
                        if callable(close_embedding):
                            close_embedding()
    return totals


def main() -> None:
    args = _parse_args()
    # Config projection and encrypted source fields are read-only: a missing
    # local/DPAPI key must fail instead of creating a new key during reindex.
    from src.security.field_crypto import _readonly_data_key_access

    with _readonly_data_key_access():
        print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
