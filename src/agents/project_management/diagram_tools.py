"""案件情報からMermaid構成図を生成するツール。"""

from __future__ import annotations

from typing import Any

from ...tools.core import tool
from sqlalchemy import select

from .common import (
    _json,
    _resolve_actor_and_project,
    _run_async,
)


def build_diagram_tools() -> list:
    """構成図生成ツール群を生成して返す。"""

    @tool
    def render_project_diagram(
        project: str = "",
        project_id: str = "",
        scope: str = "auto",
        record_table: str = "",
        max_rows: int = 200,
    ) -> str:
        """Render a Mermaid diagram from stored project data. scope: auto / system / wbs / record_table. Returns mermaid text plus source facts so the caller can refine labels or compose a diagram when no deterministic one is available."""
        from ...memory.database import get_database_manager
        from ...memory.models import (
            KnowledgeNode,
            Project,
            RecordField,
            RecordRow,
            RecordTable,
            Task,
        )
        from ...services.project_diagram import (
            build_record_table_diagram,
            build_system_diagram,
            build_wbs_diagram,
            table_kind,
        )

        async def _render():
            requested_scope = (scope or "auto").strip().casefold()
            if requested_scope not in {"auto", "system", "wbs", "record_table"}:
                raise ValueError(
                    "scope must be one of: auto, system, wbs, record_table"
                )
            row_limit = max(1, min(int(max_rows or 200), 1000))
            db = get_database_manager()
            session = await db.get_session()
            try:
                _, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if resolved_project_id is None:
                    raise ValueError("No project could be resolved.")
                project_obj = await session.get(Project, resolved_project_id)
                project_name = project_obj.name if project_obj else ""

                tables_result = await session.execute(
                    select(RecordTable)
                    .where(
                        RecordTable.project_id == resolved_project_id,
                        RecordTable.deleted_at.is_(None),
                    )
                    .order_by(RecordTable.sort_order, RecordTable.created_at)
                )
                tables: list[dict[str, Any]] = []
                for table in tables_result.scalars().all():
                    fields_result = await session.execute(
                        select(RecordField)
                        .where(
                            RecordField.table_id == table.id,
                            RecordField.deleted_at.is_(None),
                        )
                        .order_by(RecordField.sort_order)
                    )
                    rows_result = await session.execute(
                        select(RecordRow)
                        .where(
                            RecordRow.table_id == table.id,
                            RecordRow.deleted_at.is_(None),
                        )
                        .order_by(RecordRow.created_at)
                        .limit(row_limit)
                    )
                    tables.append(
                        {
                            "name": table.name,
                            "fields": [
                                {
                                    "key": field.key,
                                    "label": field.label,
                                    "field_type": field.field_type,
                                    "is_title": bool(field.is_title),
                                }
                                for field in fields_result.scalars().all()
                            ],
                            "rows": [
                                {
                                    "title": row.title,
                                    "status": row.status,
                                    "values": row.values or {},
                                }
                                for row in rows_result.scalars().all()
                            ],
                        }
                    )

                tasks_result = await session.execute(
                    select(Task)
                    .where(
                        Task.project_id == resolved_project_id,
                        Task.deleted_at.is_(None),
                        Task.archived_at.is_(None),
                    )
                    .order_by(Task.sort_order, Task.created_at)
                    .limit(row_limit)
                )
                tasks = [
                    {
                        "id": str(task.id),
                        "title": task.title,
                        "status": task.status,
                        "parent_task_id": (
                            str(task.parent_task_id)
                            if task.parent_task_id
                            else None
                        ),
                    }
                    for task in tasks_result.scalars().all()
                ]

                docs_result = await session.execute(
                    select(KnowledgeNode)
                    .where(
                        KnowledgeNode.project_id == resolved_project_id,
                        KnowledgeNode.archived_at.is_(None),
                    )
                    .order_by(KnowledgeNode.updated_at.desc())
                    .limit(50)
                )
                docs_nodes = [
                    {
                        "id": str(node.id),
                        "title": node.title,
                        "body_text": (node.body_text or "")[:500],
                    }
                    for node in docs_result.scalars().all()
                ]

                mermaid = None
                used_scope = requested_scope
                notes: list[str] = []
                if requested_scope == "record_table":
                    table_ref = (record_table or "").strip().casefold()
                    if not table_ref:
                        raise ValueError(
                            "record_table is required when scope=record_table"
                        )
                    matches = [
                        t
                        for t in tables
                        if table_ref in str(t["name"]).casefold()
                    ]
                    if not matches:
                        raise ValueError(
                            f"Record table not found: {record_table}"
                        )
                    if len(matches) > 1:
                        raise ValueError(
                            "Record table reference is ambiguous: "
                            + ", ".join(str(t["name"]) for t in matches)
                        )
                    target = matches[0]
                    mermaid = build_record_table_diagram(
                        target["name"], target["fields"], target["rows"]
                    )
                    if mermaid is None:
                        notes.append(
                            f"record table '{target['name']}' に描画可能な行がない。"
                        )
                elif requested_scope == "wbs":
                    mermaid = build_wbs_diagram(project_name, tasks)
                    if mermaid is None:
                        notes.append("対象タスクが存在しない。")
                elif requested_scope == "system":
                    mermaid = build_system_diagram(project_name, tables)
                    if mermaid is None:
                        notes.append(
                            "機器一覧/接続一覧に該当するrecord tableがない。"
                            "Docs本文を元に構成図を作成すること。"
                        )
                else:
                    mermaid = build_system_diagram(project_name, tables)
                    used_scope = "system"
                    if mermaid is None and tasks:
                        mermaid = build_wbs_diagram(project_name, tasks)
                        used_scope = "wbs"
                    if mermaid is None:
                        used_scope = "none"
                        notes.append(
                            "構造化データから図を生成できなかった。"
                            "Docs本文を元に構成図を作成すること。"
                        )

                return {
                    "project_id": str(resolved_project_id),
                    "project_name": project_name,
                    "requested_scope": requested_scope,
                    "scope": used_scope,
                    "mermaid": mermaid,
                    "sources": {
                        "docs_nodes": docs_nodes,
                        "record_tables": [
                            {
                                "name": t["name"],
                                "kind": table_kind(t["name"]) or "other",
                                "row_count": len(t["rows"]),
                            }
                            for t in tables
                        ],
                        "task_count": len(tasks),
                    },
                    "notes": notes,
                }
            finally:
                await session.close()

        try:
            return _json(_run_async(_render()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    return [render_project_diagram]
