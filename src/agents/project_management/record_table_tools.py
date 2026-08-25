"""プロジェクトスコープのレコードテーブル（DB風テーブル）関連ツール。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from ...tools.core import tool
from sqlalchemy import func, select, update

from .common import (
    _run_async,
    _resolve_actor_and_project,
    _parse_datetime,
    _parse_ids,
    _json,
    _parse_json_array,
    _unique_record_field_key,
    _normalize_record_columns,
    _materialize_record_row,
    _row_payload_to_values,
    _resolve_record_table,
)


def build_record_table_tools() -> list:
    """プロジェクトスコープのレコードテーブル（DB風テーブル）関連ツールのツール群を生成して返す。"""

    @tool
    def list_record_tables(project: str = "", project_id: str = "") -> str:
        """List DB-style record tables in a project. Use this before updating an existing table."""
        from ...memory.database import get_database_manager
        from ...memory.models import RecordRow, RecordTable

        async def _list():
            db = get_database_manager()
            session = await db.get_session()
            try:
                _, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                    permission="read",
                )
                result = await session.execute(
                    select(
                        RecordTable,
                        func.count(RecordRow.id).label("row_count"),
                    )
                    .outerjoin(
                        RecordRow,
                        (RecordRow.table_id == RecordTable.id)
                        & (RecordRow.deleted_at.is_(None)),
                    )
                    .where(
                        RecordTable.project_id == resolved_project_id,
                        RecordTable.deleted_at.is_(None),
                    )
                    .group_by(RecordTable.id)
                    .order_by(RecordTable.sort_order, RecordTable.created_at)
                )
                return [
                    {
                        "id": str(table.id),
                        "name": table.name,
                        "description": table.description,
                        "row_count": int(row_count or 0),
                        "filer_name": f"{table.name}.dbtable",
                    }
                    for table, row_count in result.all()
                ]
            finally:
                await session.close()

        return _json(_run_async(_list()))

    @tool
    def create_record_table(
        table_name: str,
        columns_json: str = "",
        rows_json: str = "",
        project: str = "",
        project_id: str = "",
        description: str = "",
    ) -> str:
        """Create a project-scoped DB table. `columns_json` is a JSON array of strings or objects like {"label":"期限","type":"date"}. `rows_json` is a JSON array of row objects."""
        from ...memory.database import get_database_manager
        from ...memory.models import RecordField, RecordRow, RecordTable, RecordView

        async def _create():
            rows_payload = _parse_json_array(rows_json, "rows_json")
            columns = _normalize_record_columns(columns_json, rows_payload)
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                    permission="write",
                )
                if resolved_project_id is None:
                    raise ValueError("No project could be resolved for the record table.")

                max_sort_result = await session.execute(
                    select(func.max(RecordTable.sort_order)).where(
                        RecordTable.project_id == resolved_project_id
                    )
                )
                sort_order = float(max_sort_result.scalar_one_or_none() or 0) + 1
                table = RecordTable(
                    project_id=resolved_project_id,
                    name=table_name.strip() or "New table",
                    description=description.strip() or None,
                    sort_order=sort_order,
                    created_by=user_id,
                    table_metadata={"source": "agent"},
                )
                session.add(table)
                await session.flush()

                fields = []
                reserved_field_keys: set[str] = set()
                for index, column in enumerate(columns):
                    key = await _unique_record_field_key(
                        session,
                        table.id,
                        column["label"],
                        reserved_field_keys,
                    )
                    reserved_field_keys.add(key)
                    field = RecordField(
                        table_id=table.id,
                        key=key,
                        label=column["label"],
                        field_type=column["field_type"],
                        sort_order=index,
                        is_title=index == 0,
                        options={},
                    )
                    session.add(field)
                    fields.append(field)
                await session.flush()

                session.add(
                    RecordView(
                        table_id=table.id,
                        name="Grid",
                        view_type="grid",
                        config={},
                        sort_order=0,
                        created_by=user_id,
                    )
                )

                created_rows = []
                for row_payload in rows_payload:
                    values = _row_payload_to_values(row_payload, fields)
                    materialized = _materialize_record_row(values, fields)
                    row = RecordRow(
                        table_id=table.id,
                        project_id=resolved_project_id,
                        created_by=user_id,
                        values=values,
                        title=materialized["title"],
                        search_text=materialized["search_text"],
                        row_metadata={"source": "agent"},
                    )
                    session.add(row)
                    created_rows.append(row)

                await session.commit()
                return {
                    "success": True,
                    "project_id": str(resolved_project_id),
                    "table_id": str(table.id),
                    "table_name": table.name,
                    "filer_name": f"{table.name}.dbtable",
                    "field_count": len(fields),
                    "row_count": len(created_rows),
                    "message": "Created. It appears as a .dbtable item in the project workspace filer.",
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_create()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def append_record_rows(
        record_table: str,
        rows_json: str,
        project: str = "",
        project_id: str = "",
    ) -> str:
        """Append rows to an existing project DB table. `record_table` accepts a table id or table name. `rows_json` is a JSON array of row objects."""
        from ...memory.database import get_database_manager
        from ...memory.models import RecordField, RecordRow

        async def _append():
            rows_payload = _parse_json_array(rows_json, "rows_json")
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                    permission="write",
                )
                if resolved_project_id is None:
                    raise ValueError("No project could be resolved for the record table.")

                table = await _resolve_record_table(
                    session,
                    resolved_project_id,
                    record_table,
                )
                fields_result = await session.execute(
                    select(RecordField)
                    .where(
                        RecordField.table_id == table.id,
                        RecordField.deleted_at.is_(None),
                    )
                    .order_by(RecordField.sort_order, RecordField.created_at)
                )
                fields = list(fields_result.scalars().all())

                known_labels = {field.label for field in fields}
                known_keys = {field.key for field in fields}
                reserved_field_keys = set(known_keys)
                for row_payload in rows_payload:
                    if not isinstance(row_payload, dict):
                        continue
                    for raw_key in row_payload.keys():
                        label = str(raw_key).strip()
                        if label and label not in known_labels and label not in known_keys:
                            key = await _unique_record_field_key(
                                session,
                                table.id,
                                label,
                                reserved_field_keys,
                            )
                            reserved_field_keys.add(key)
                            field = RecordField(
                                table_id=table.id,
                                key=key,
                                label=label,
                                field_type="text",
                                sort_order=len(fields),
                                is_title=len(fields) == 0,
                                options={},
                            )
                            session.add(field)
                            fields.append(field)
                            known_labels.add(label)
                            known_keys.add(key)
                await session.flush()

                created_rows = []
                for row_payload in rows_payload:
                    values = _row_payload_to_values(row_payload, fields)
                    materialized = _materialize_record_row(values, fields)
                    row = RecordRow(
                        table_id=table.id,
                        project_id=resolved_project_id,
                        created_by=user_id,
                        values=values,
                        title=materialized["title"],
                        search_text=materialized["search_text"],
                        row_metadata={"source": "agent"},
                    )
                    session.add(row)
                    created_rows.append(row)

                await session.commit()
                return {
                    "success": True,
                    "project_id": str(resolved_project_id),
                    "table_id": str(table.id),
                    "table_name": table.name,
                    "added_rows": len(created_rows),
                    "field_count": len(fields),
                    "filer_name": f"{table.name}.dbtable",
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_append()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def update_record_row(
        row_id: str,
        values_json: str = "",
        title: str = "",
        status: str = "",
        due_at: str = "",
    ) -> str:
        """Patch a project DB table row. `values_json` must be a JSON object keyed by column key or label."""
        from ...memory.database import get_database_manager
        from ...memory.models import RecordField, RecordRow

        async def _update():
            patch_text = (values_json or "").strip()
            raw_patch: dict[str, Any] = {}
            if patch_text:
                parsed_patch = json.loads(patch_text)
                if not isinstance(parsed_patch, dict):
                    raise ValueError("values_json must be a JSON object.")
                raw_patch = parsed_patch

            db = get_database_manager()
            session = await db.get_session()
            try:
                row = await session.get(RecordRow, UUID(row_id))
                if row is None or row.deleted_at is not None:
                    raise ValueError("Record row not found.")
                await _resolve_actor_and_project(
                    session,
                    project_id=str(row.project_id),
                    permission="write",
                )
                fields_result = await session.execute(
                    select(RecordField)
                    .where(
                        RecordField.table_id == row.table_id,
                        RecordField.deleted_at.is_(None),
                    )
                    .order_by(RecordField.sort_order, RecordField.created_at)
                )
                fields = list(fields_result.scalars().all())
                values = dict(row.values or {})
                values.update(_row_payload_to_values(raw_patch, fields))
                materialized = _materialize_record_row(values, fields)
                row.values = values
                if title:
                    row.title = title[:500]
                else:
                    row.title = materialized["title"]
                if status:
                    row.status = status[:64]
                if due_at:
                    row.due_at = _parse_datetime(due_at)
                row.search_text = materialized["search_text"]
                row.updated_at = datetime.utcnow()
                await session.commit()
                return {
                    "success": True,
                    "row": {
                        "id": str(row.id),
                        "table_id": str(row.table_id),
                        "project_id": str(row.project_id),
                        "values": row.values or {},
                        "title": row.title,
                        "status": row.status,
                        "due_at": row.due_at.isoformat() if row.due_at else None,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    },
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_update()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def delete_record_rows(row_ids: str) -> str:
        """Soft-delete one or more project DB table rows. `row_ids` is a comma-separated list of UUIDs."""
        from ...memory.database import get_database_manager
        from ...memory.models import RecordRow

        async def _delete():
            ids = _parse_ids(row_ids)
            if not ids:
                raise ValueError("row_ids is required.")
            db = get_database_manager()
            session = await db.get_session()
            try:
                result = await session.execute(
                    select(RecordRow).where(
                        RecordRow.id.in_(ids),
                        RecordRow.deleted_at.is_(None),
                    )
                )
                rows = list(result.scalars().all())
                if not rows:
                    raise ValueError("No active record rows found.")
                for project_id_value in {row.project_id for row in rows}:
                    await _resolve_actor_and_project(
                        session,
                        project_id=str(project_id_value),
                        permission="write",
                    )
                now = datetime.utcnow()
                for row in rows:
                    row.deleted_at = now
                    row.updated_at = now
                await session.commit()
                return {
                    "success": True,
                    "deleted_count": len(rows),
                    "row_ids": [str(row.id) for row in rows],
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_delete()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def delete_record_table(
        record_table: str,
        project: str = "",
        project_id: str = "",
    ) -> str:
        """Soft-delete a project DB table, its fields, and its rows."""
        from ...memory.database import get_database_manager
        from ...memory.models import RecordField, RecordRow, RecordTable

        async def _delete():
            db = get_database_manager()
            session = await db.get_session()
            try:
                _, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                    permission="write",
                )
                if resolved_project_id is None:
                    raise ValueError("No project could be resolved for the record table.")
                table = await _resolve_record_table(
                    session,
                    resolved_project_id,
                    record_table,
                )
                now = datetime.utcnow()
                table.deleted_at = now
                table.updated_at = now
                await session.execute(
                    update(RecordField)
                    .where(
                        RecordField.table_id == table.id,
                        RecordField.deleted_at.is_(None),
                    )
                    .values(deleted_at=now, updated_at=now)
                )
                await session.execute(
                    update(RecordRow)
                    .where(
                        RecordRow.table_id == table.id,
                        RecordRow.deleted_at.is_(None),
                    )
                    .values(deleted_at=now, updated_at=now)
                )
                await session.commit()
                return {
                    "success": True,
                    "table_id": str(table.id),
                    "table_name": table.name,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_delete()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    return [
        list_record_tables,
        create_record_table,
        append_record_rows,
        update_record_row,
        delete_record_rows,
        delete_record_table,
    ]
