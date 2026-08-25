r"""Move App README Docs nodes under the canonical ``アプリ`` hub.

The default mode is read-only.  Use ``--apply`` after reviewing the plan:

    venv\Scripts\python.exe scripts\migrations\migrate_app_readme_docs.py
    venv\Scripts\python.exe scripts\migrations\migrate_app_readme_docs.py --apply

Hierarchy, App README pointers, derived search-index fields, and migration
revisions are repaired. README bodies, node IDs, display properties, and
App/Project bindings are preserved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from src.memory.database import DatabaseManager  # noqa: E402
from src.memory.models import (  # noqa: E402
    App,
    DocsLibrary,
    KnowledgeNode,
    KnowledgeSearchIndex,
)
from src.services.app_service import (  # noqa: E402
    APP_DOCS_ROOT_SYSTEM_KEY,
    _ensure_app_docs_root,
)
from src.services.docs_graph_service import DocsGraphService  # noqa: E402


async def migrate(*, apply: bool) -> dict[str, Any]:
    manager = DatabaseManager()
    try:
        async with manager.SessionLocal() as session:
            apps = list((await session.scalars(select(App))).all())
            apps_by_id = {app.id: app for app in apps}
            nodes = list(
                (
                    await session.scalars(
                        select(KnowledgeNode)
                        .where(
                            KnowledgeNode.node_type == "app_readme",
                        )
                        .order_by(KnowledgeNode.created_at)
                    )
                ).all()
            )
            # Keep orphaned/archived App rows in the repair plan as well.  They
            # should not remain as top-level Docs pages after the fix.
            nodes_by_docs_library: dict[Any, list[KnowledgeNode]] = defaultdict(list)
            for node in nodes:
                nodes_by_docs_library[node.docs_library_id].append(node)

            docs_library_ids = list(nodes_by_docs_library)
            docs_libraries = {
                docs_library.id: docs_library
                for docs_library in (
                    await session.scalars(
                        select(DocsLibrary).where(
                            DocsLibrary.id.in_(docs_library_ids or [None])
                        )
                    )
                ).all()
            }

            owner_ids = {app.owner_user_id for app in apps if app.owner_user_id is not None}
            owner_docs_libraries = list(
                (
                    await session.scalars(
                        select(DocsLibrary).where(
                            DocsLibrary.owner_user_id.in_(owner_ids or [None])
                        )
                    )
                ).all()
            )
            owner_docs_library_by_owner = {
                docs_library.owner_user_id: docs_library.id
                for docs_library in owner_docs_libraries
            }

            plan: list[dict[str, Any]] = []
            nodes_by_app: dict[Any, list[KnowledgeNode]] = defaultdict(list)
            for node in nodes:
                if node.app_id is not None:
                    nodes_by_app[node.app_id].append(node)
            canonical_node_by_app: dict[Any, KnowledgeNode] = {}
            for app_id, candidates in nodes_by_app.items():
                app = apps_by_id.get(app_id)
                owner_docs_library_id = (
                    owner_docs_library_by_owner.get(app.owner_user_id) if app else None
                )
                canonical_node_by_app[app_id] = min(
                    candidates,
                    key=lambda node: (
                        0
                        if owner_docs_library_id is not None
                        and node.docs_library_id == owner_docs_library_id
                        else 1,
                        0 if node.archived_at is None else 1,
                        0 if app is not None and app.readme_node_id == node.id else 1,
                        node.created_at or datetime.max,
                    ),
                )
            pointer_fixes = [
                app
                for app_id, app in apps_by_id.items()
                if (canonical := canonical_node_by_app.get(app_id)) is not None
                and app.readme_node_id != canonical.id
            ]
            for docs_library_id, docs_library_nodes in nodes_by_docs_library.items():
                root = await session.scalar(
                    select(KnowledgeNode)
                    .where(
                        KnowledgeNode.docs_library_id == docs_library_id,
                        KnowledgeNode.system_key == APP_DOCS_ROOT_SYSTEM_KEY,
                    )
                    .limit(1)
                )
                moves = [
                    node
                    for node in docs_library_nodes
                    if root is None
                    or node.parent_id != root.id
                    or node.root_page_id != root.id
                    or node.project_id is not None
                ]
                plan.append(
                    {
                        "workspace_id": str(docs_library_id),
                        "workspace_owner_id": str(
                            docs_libraries[docs_library_id].owner_user_id
                        )
                        if docs_library_id in docs_libraries
                        and docs_libraries[docs_library_id].owner_user_id
                        else None,
                        "root_id": str(root.id) if root else None,
                        "app_readme_count": len(docs_library_nodes),
                        "move_count": len(moves),
                        "node_ids": [str(node.id) for node in moves],
                        "pointer_fix_count": sum(
                            1
                            for node in docs_library_nodes
                            if canonical_node_by_app.get(node.app_id) is node
                            and apps_by_id.get(node.app_id) in pointer_fixes
                        ),
                    }
                )

            result: dict[str, Any] = {
                "apply": apply,
                "app_readme_count": len(nodes),
                "app_pointer_fix_count": len(pointer_fixes),
                "workspaces": plan,
            }
            if not apply:
                await session.rollback()
                return result

            moved = 0
            pointer_updated = 0
            for docs_library_id, docs_library_nodes in nodes_by_docs_library.items():
                docs_library = docs_libraries.get(docs_library_id)
                root = await _ensure_app_docs_root(
                    session,
                    docs_library_id=docs_library_id,
                    user_id=docs_library.owner_user_id if docs_library else None,
                )
                graph = DocsGraphService(session)
                for node in docs_library_nodes:
                    changed = (
                        node.parent_id != root.id
                        or node.root_page_id != root.id
                        or node.project_id is not None
                    )
                    node.parent_id = root.id
                    node.root_page_id = root.id
                    node.project_id = None
                    await graph._propagate_root_page(node)
                    if changed:
                        await graph.record_node_change(
                            node,
                            docs_library.owner_user_id if docs_library else None,
                            "App Docs階層を移行",
                            source_refs=[
                                {"source": "app_docs_migration", "app_id": str(node.app_id)}
                            ],
                        )
                        moved += 1
                    else:
                        search_index = await session.get(KnowledgeSearchIndex, node.id)
                        index_matches = bool(
                            search_index
                            and search_index.docs_library_id == node.docs_library_id
                            and search_index.project_id == node.project_id
                            and search_index.title_text == node.title
                            and search_index.body_text_plain == node.body_text
                        )
                        if not index_matches:
                            await graph.upsert_search_index(node)

            for app in pointer_fixes:
                canonical = canonical_node_by_app.get(app.id)
                if canonical is None:
                    continue
                app.readme_node_id = canonical.id
                pointer_updated += 1

            await session.commit()
            result["moved_count"] = moved
            result["app_pointer_updated_count"] = pointer_updated
            return result
    finally:
        await manager.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the idempotent hierarchy repair. Without this flag the DB is read-only.",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(migrate(apply=args.apply)), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
