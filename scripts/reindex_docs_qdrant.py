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

from src.memory.database import DatabaseManager
from src.memory.models import DocsLibrary
from src.rag.config import EmbeddingConfig, QdrantConfig, get_rag_config
from src.rag.docs_index import DocsIndexService


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
    config = get_rag_config()
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

    db = DatabaseManager()
    if not await db.initialize():
        raise RuntimeError("Database migrations must complete before reindexing Docs")
    session = await db.get_session()
    totals = {"libraries": 0, "nodes": 0, "reindexed": 0, "removed": 0}
    try:
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
            totals["libraries"] += 1
            totals["nodes"] += int(result.get("total", 0))
            totals["reindexed"] += int(result.get("reindexed", 0))
            totals["removed"] += int(result.get("removed", 0))
    finally:
        await session.close()
        await db.close()
        if service.client is not None and hasattr(service.client, "close"):
            service.client.close()
    return totals


def main() -> None:
    args = _parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
