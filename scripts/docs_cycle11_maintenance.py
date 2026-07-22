"""Docs cycle 11/12 data maintenance.

This script is intentionally data-only. Schema changes belong in Alembic
migrations; keep DDL out of maintenance and seed scripts.

Run:
  venv\\Scripts\\python.exe scripts\\docs_cycle11_maintenance.py
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from maintain_project_information_docs import (  # noqa: E402
    DB_KIND,
    NODE_BODY_JSON_AAD,
    NODE_BODY_TEXT_AAD,
    connect,
    encrypt_json,
    encrypt_text,
    json_param,
    load_data_key,
    load_dotenv,
)

if DB_KIND == "psycopg":
    from psycopg.rows import dict_row  # type: ignore
else:
    from psycopg2.extras import RealDictCursor  # type: ignore


TASK_FIELD_KEYS = ["task_status", "task_due", "task_start", "task_priority", "task_assignee", "task_project"]
PROCEDURE_SECTION_TITLE = "手順・ルール"
PROCEDURE_PAGES = [
    {
        "title": "FW申請フロー",
        "aliases": ["FW申請", "ファイアウォール申請"],
        "lines": [
            "申請対象の通信元・通信先・ポート・利用期間を申請前に一覧化する。",
            "HUB運用窓口へ事前確認を行い、受付番号26-9001形式で管理する。",
            "承認後は反映予定日と戻し手順を案件ページへ記録する。",
        ],
    },
    {
        "title": "LB申請フロー",
        "aliases": ["LB申請", "ロードバランサ申請"],
        "lines": [
            "仮想サービス名、振分先、監視URL、切替時間帯を申請票に記載する。",
            "SUBA検証系で疎通確認を完了してから本番DC1の変更枠を予約する。",
            "切替後は監視アラートと利用部門の確認結果を同じページへ追記する。",
        ],
    },
    {
        "title": "intra-martでのチケット起票手順",
        "aliases": ["イントラ", "チケット", "intra-mart"],
        "lines": [
            "案件名、申請種別、希望日、影響範囲を入力し、関連資料を添付する。",
            "一時保存ではなく申請番号が発番されたことを確認して案件ページへリンクする。",
            "差し戻し時は差し戻し理由を決定事項ではなく課題として残す。",
        ],
    },
    {
        "title": "常駐勤怠ルール",
        "aliases": ["勤怠", "常駐ルール"],
        "lines": [
            "常駐日の入退館時刻は当日中に勤怠表へ記録する。",
            "半日作業は午前・午後の区分と対応チケット番号を必ず併記する。",
            "月末締め前に未記入日と深夜作業申請の有無を確認する。",
        ],
    },
    {
        "title": "月末処理手順",
        "aliases": ["月末", "締め処理"],
        "lines": [
            "月末3営業日前に未完了申請、保留チケット、翌月持越し課題を抽出する。",
            "作業実績と申請番号の対応表を更新し、案件ページの今週の作業を確認する。",
            "締め後に翌月初回定例の議題へ持越し事項を登録する。",
        ],
    },
]
DEVICE_ROWS = [
    {"title": "HUB-FW-01", "model": "FWA-2600", "quantity": "1", "usage": "外部接続境界", "location": "HUB第1ラック", "asset": "FW-26-001", "maintenance": "2026-12-31"},
    {"title": "SUBA-LB-02", "model": "LBA-1800", "quantity": "2", "usage": "申請系負荷分散", "location": "SUBA検証室", "asset": "LB-26-014", "maintenance": "2027-03-31"},
    {"title": "DC1-MON-03", "model": "MNA-900", "quantity": "1", "usage": "変更後監視", "location": "DC1監視棚", "asset": "MN-26-007", "maintenance": "2026-11-30"},
]


def fetchone(cur) -> dict[str, Any] | None:
    row = cur.fetchone()
    return dict(row) if row else None


def fetchall(cur) -> list[dict[str, Any]]:
    return [dict(row) for row in cur.fetchall()]


def schema_status(cur) -> dict[str, Any]:
    cur.execute(
        """
        select column_name
          from information_schema.columns
         where table_name = 'knowledge_nodes' and column_name = 'aliases'
        """
    )
    alias_ready = cur.fetchone() is not None
    cur.execute(
        """
        select is_nullable
          from information_schema.columns
         where table_name = 'tasks' and column_name = 'priority'
        """
    )
    priority_row = fetchone(cur)
    cur.execute("select version_num from alembic_version limit 1")
    version = fetchone(cur)
    return {
        "aliases_column_present": alias_ready,
        "tasks_priority_nullable": priority_row["is_nullable"] == "YES" if priority_row else None,
        "alembic_version": version["version_num"] if version else None,
    }


def tag_snapshot(cur, workspace_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        select st.id, st.name, st.system_key, count(nst.node_id)::int as node_count
          from knowledge_supertags st
          left join knowledge_node_supertags nst on nst.supertag_id = st.id
         where st.workspace_id = %s
           and (lower(st.name) in ('risk', 'decision') or st.name like %s or st.system_key in ('risk', 'decision', 'procedure', 'meeting_note', 'meeting_minutes', 'device', 'person'))
         group by st.id, st.name, st.system_key
         order by lower(st.name), st.system_key nulls first
        """,
        (workspace_id, "%統合済み%"),
    )
    return fetchall(cur)


def ensure_tag(
    cur,
    workspace_id: str,
    *,
    name: str,
    system_key: str,
    base_type: str,
    color: str,
    icon: str | None = None,
    template: dict[str, Any] | None = None,
    title_template: str | None = None,
) -> str:
    cur.execute(
        "select id from knowledge_supertags where workspace_id = %s and system_key = %s",
        (workspace_id, system_key),
    )
    row = fetchone(cur)
    if row:
        tag_id = row["id"]
        cur.execute(
            """
            update knowledge_supertags
               set name = %s, base_type = %s, color = %s, icon = coalesce(%s, icon),
                   template_json = coalesce(%s, template_json),
                   title_template = coalesce(%s, title_template),
                   updated_at = now()
             where id = %s
            """,
            (name, base_type, color, icon, json_param(template) if template is not None else None, title_template, tag_id),
        )
        return str(tag_id)
    cur.execute(
        "select id from knowledge_supertags where workspace_id = %s and lower(name) = lower(%s) limit 1",
        (workspace_id, name),
    )
    row = fetchone(cur)
    if row:
        tag_id = row["id"]
        cur.execute(
            """
            update knowledge_supertags
               set system_key = %s, name = %s, base_type = %s, color = %s, icon = coalesce(%s, icon),
                   template_json = coalesce(%s, template_json),
                   title_template = coalesce(%s, title_template),
                   updated_at = now()
             where id = %s
            """,
            (system_key, name, base_type, color, icon, json_param(template) if template is not None else None, title_template, tag_id),
        )
        return str(tag_id)
    tag_id = str(uuid.uuid4())
    cur.execute(
        """
        insert into knowledge_supertags
          (id, workspace_id, system_key, name, base_type, icon, color, template_json, title_template)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (tag_id, workspace_id, system_key, name, base_type, icon or system_key, color, json_param(template or {}), title_template),
    )
    return tag_id


def ensure_field(cur, workspace_id: str, tag_id: str, *, name: str, system_key: str, field_type: str, sort_order: float = 0, options: list[str] | None = None) -> str:
    cur.execute(
        "select id from knowledge_fields where workspace_id = %s and system_key = %s limit 1",
        (workspace_id, system_key),
    )
    row = fetchone(cur)
    if row:
        field_id = row["id"]
        cur.execute(
            """
            update knowledge_fields
               set supertag_id = %s, name = %s, field_type = %s, sort_order = %s,
                   options_json = coalesce(%s, options_json), updated_at = now()
             where id = %s
            """,
            (tag_id, name, field_type, sort_order, json_param({"values": options}) if options is not None else None, field_id),
        )
        return str(field_id)
    cur.execute(
        "select id from knowledge_fields where workspace_id = %s and supertag_id = %s and lower(name) = lower(%s) limit 1",
        (workspace_id, tag_id, name),
    )
    row = fetchone(cur)
    if row:
        field_id = row["id"]
        cur.execute(
            """
            update knowledge_fields
               set system_key = %s, name = %s, field_type = %s, sort_order = %s,
                   options_json = coalesce(%s, options_json), updated_at = now()
             where id = %s
            """,
            (system_key, name, field_type, sort_order, json_param({"values": options}) if options is not None else None, field_id),
        )
        return str(field_id)
    field_id = str(uuid.uuid4())
    cur.execute(
        """
        insert into knowledge_fields
          (id, workspace_id, supertag_id, system_key, name, field_type, options_json, sort_order)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (field_id, workspace_id, tag_id, system_key, name, field_type, json_param({"values": options or []}), sort_order),
    )
    return field_id


def ensure_node(
    cur,
    *,
    workspace_id: str,
    parent_id: str | None,
    root_page_id: str | None,
    project_id: str | None,
    title: str,
    body: str,
    data_key: bytes,
    created_by: str | None,
    updated_by: str | None,
    node_type: str = "node",
    body_json: dict[str, Any] | None = None,
    query_json: dict[str, Any] | None = None,
    view_json: dict[str, Any] | None = None,
    sort_order: float = 0,
    aliases: list[str] | None = None,
) -> tuple[str, bool]:
    cur.execute(
        """
        select id from knowledge_nodes
         where workspace_id = %s
           and parent_id is not distinct from %s
           and title = %s
           and archived_at is null
         limit 1
        """,
        (workspace_id, parent_id, title),
    )
    existing = fetchone(cur)
    if existing:
        node_id = existing["id"]
        if aliases is not None:
            cur.execute("update knowledge_nodes set aliases = %s, updated_at = now() where id = %s", (json_param(aliases), node_id))
        return str(node_id), False
    node_id = str(uuid.uuid4())
    cur.execute(
        """
        insert into knowledge_nodes
          (id, workspace_id, parent_id, root_page_id, project_id, title, aliases, body_text, body_json,
           node_type, query_json, view_json, sort_order, created_by, updated_by)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            node_id,
            workspace_id,
            parent_id,
            root_page_id or node_id,
            project_id,
            title,
            json_param(aliases or []),
            encrypt_text(body, NODE_BODY_TEXT_AAD, data_key),
            json_param(encrypt_json(body_json or {"format": "doc_block", "block_type": "paragraph"}, NODE_BODY_JSON_AAD, data_key)),
            node_type,
            json_param(query_json) if query_json is not None else None,
            json_param(view_json or {}),
            sort_order,
            created_by,
            updated_by,
        ),
    )
    cur.execute(
        """
        insert into knowledge_search_index (node_id, workspace_id, project_id, title_text, body_text_plain)
        values (%s, %s, %s, %s, %s)
        on conflict (node_id) do update set title_text = excluded.title_text, body_text_plain = excluded.body_text_plain
        """,
        (node_id, workspace_id, project_id, title, body),
    )
    return node_id, True


def attach_tag(cur, node_id: str, tag_id: str, created_by: str | None) -> None:
    cur.execute(
        """
        insert into knowledge_node_supertags (node_id, supertag_id, created_by)
        values (%s, %s, %s)
        on conflict (node_id, supertag_id) do nothing
        """,
        (node_id, tag_id, created_by),
    )


def merge_duplicate_tag(cur, workspace_id: str, english: str, japanese: str, system_key: str) -> dict[str, Any]:
    target_id = ensure_tag(cur, workspace_id, name=japanese, system_key=system_key, base_type="note", color="#64748b")
    cur.execute(
        """
        select id, name from knowledge_supertags
         where workspace_id = %s
           and id <> %s
           and (lower(name) = lower(%s) or lower(name) = lower(%s) or name like %s)
        """,
        (workspace_id, target_id, english, japanese, f"{english} (%"),
    )
    sources = fetchall(cur)
    moved = 0
    deleted: list[str] = []
    for source in sources:
        cur.execute(
            """
            insert into knowledge_node_supertags (node_id, supertag_id, created_by)
            select node_id, %s, created_by
              from knowledge_node_supertags
             where supertag_id = %s
            on conflict (node_id, supertag_id) do nothing
            """,
            (target_id, source["id"]),
        )
        moved += cur.rowcount
        cur.execute("delete from knowledge_node_supertags where supertag_id = %s", (source["id"],))
        cur.execute("delete from knowledge_supertags where id = %s", (source["id"],))
        deleted.append(source["name"])
    cur.execute("update knowledge_supertags set system_key = %s, name = %s where id = %s", (system_key, japanese, target_id))
    return {"target": target_id, "sources_deleted": deleted, "relations_moved": moved}


def cleanup_orphan_english_tags(cur, workspace_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        select st.id, st.name, count(nst.node_id)::int as node_count
          from knowledge_supertags st
          left join knowledge_node_supertags nst on nst.supertag_id = st.id
         where st.workspace_id = %s
           and (lower(st.name) in ('risk', 'decision') or st.name like %s)
         group by st.id, st.name
        having count(nst.node_id) = 0
        """,
        (workspace_id, "%統合済み%"),
    )
    rows = fetchall(cur)
    for row in rows:
        cur.execute("delete from knowledge_supertags where id = %s", (row["id"],))
    return rows


def ensure_cycle_tags(cur, workspace_id: str) -> dict[str, Any]:
    meeting_template = {
        "blocks": [
            {"text": "日時", "depth": 0},
            {"text": "出席者", "depth": 0},
            {"text": "議題", "depth": 0},
            {"text": "メモ", "depth": 0},
        ]
    }
    decision_merge = merge_duplicate_tag(cur, workspace_id, "Decision", "決定", "decision")
    risk_merge = merge_duplicate_tag(cur, workspace_id, "Risk", "リスク", "risk")
    result = {
        "task": ensure_tag(cur, workspace_id, name="タスク", system_key="task", base_type="task", color="#22c55e", icon="check-square"),
        "meeting_note": ensure_tag(cur, workspace_id, name="議事メモ", system_key="meeting_note", base_type="meeting", color="#0ea5e9", icon="notebook", template=meeting_template, title_template="{project} {date} 議事メモ"),
        "meeting_minutes": ensure_tag(cur, workspace_id, name="議事録", system_key="meeting_minutes", base_type="meeting", color="#14b8a6", icon="scroll"),
        "procedure": ensure_tag(cur, workspace_id, name="手順", system_key="procedure", base_type="note", color="#8b5cf6", icon="list-checks"),
        "person": ensure_tag(cur, workspace_id, name="人物", system_key="person", base_type="person", color="#f59e0b", icon="user"),
        "device": ensure_tag(cur, workspace_id, name="Device", system_key="device", base_type="note", color="#64748b", icon="server"),
        "decision": decision_merge["target"],
        "risk": risk_merge["target"],
        "decision_merge": decision_merge,
        "risk_merge": risk_merge,
        "orphan_english_deleted": cleanup_orphan_english_tags(cur, workspace_id),
    }
    device_id = result["device"]
    result["device_fields"] = {
        "model": ensure_field(cur, workspace_id, device_id, name="型番", system_key="device_model", field_type="text", sort_order=0),
        "quantity": ensure_field(cur, workspace_id, device_id, name="数量", system_key="device_quantity", field_type="number", sort_order=1),
        "usage": ensure_field(cur, workspace_id, device_id, name="用途", system_key="device_usage", field_type="text", sort_order=2),
        "location": ensure_field(cur, workspace_id, device_id, name="設置場所", system_key="device_location", field_type="text", sort_order=3),
        "asset": ensure_field(cur, workspace_id, device_id, name="管理番号", system_key="device_asset_number", field_type="text", sort_order=4),
        "maintenance": ensure_field(cur, workspace_id, device_id, name="保守期限", system_key="device_maintenance_due", field_type="date", sort_order=5),
    }
    meeting_id = result["meeting_note"]
    ensure_field(cur, workspace_id, meeting_id, name="日時", system_key="meeting_datetime", field_type="date", sort_order=0)
    ensure_field(cur, workspace_id, meeting_id, name="案件", system_key="meeting_project", field_type="reference", sort_order=1)
    ensure_field(cur, workspace_id, meeting_id, name="関連タスク", system_key="meeting_related_task", field_type="reference", sort_order=2)
    return result


def migrate_task_canonical(cur, workspace_id: str, task_tag_id: str) -> dict[str, Any]:
    cur.execute(
        """
        select n.id, n.project_id, n.title, n.created_by
          from knowledge_nodes n
          join knowledge_node_supertags nst on nst.node_id = n.id
         where n.workspace_id = %s
           and nst.supertag_id = %s
           and n.archived_at is null
           and n.project_id is not null
           and not exists (
             select 1 from tasks t where t.knowledge_node_id = n.id and t.deleted_at is null
           )
        """,
        (workspace_id, task_tag_id),
    )
    missing = fetchall(cur)
    for node in missing:
        cur.execute(
            """
            insert into tasks
              (id, project_id, knowledge_node_id, title, status, priority, source, created_by, task_metadata, created_at, updated_at)
            values (%s, %s, %s, %s, 'todo', null, 'docs', %s, %s, now(), now())
            """,
            (
                str(uuid.uuid4()),
                node["project_id"],
                node["id"],
                node["title"] or "Untitled task",
                node["created_by"],
                json_param({"source": "docs", "knowledge_node_id": node["id"], "cycle12_migration": True}),
            ),
        )
    cur.execute(
        """
        delete from knowledge_field_values fv
        using knowledge_fields f, knowledge_node_supertags nst
        where fv.field_id = f.id
          and fv.node_id = nst.node_id
          and nst.supertag_id = %s
          and f.system_key = any(%s)
        """,
        (task_tag_id, TASK_FIELD_KEYS),
    )
    deleted_field_values = cur.rowcount
    cur.execute(
        """
        select count(*)::int as count
          from knowledge_field_values fv
          join knowledge_fields f on f.id = fv.field_id
          join knowledge_node_supertags nst on nst.node_id = fv.node_id
         where nst.supertag_id = %s and f.system_key = any(%s)
        """,
        (task_tag_id, TASK_FIELD_KEYS),
    )
    remaining = int(fetchone(cur)["count"])
    cur.execute(
        """
        select count(*)::int as count
          from tasks
         where task_metadata->>'cycle12_migration' = 'true'
           and (end_at is not null or start_at is not null or priority is not null)
        """
    )
    auto_filled = int(fetchone(cur)["count"])
    return {
        "missing_task_nodes": len(missing),
        "tasks_created": len(missing),
        "deleted_duplicate_field_values": deleted_field_values,
        "remaining_duplicate_task_field_values": remaining,
        "auto_filled_blank_date_or_priority": auto_filled,
    }


def fix_meeting_related_task_values(cur) -> dict[str, int]:
    cur.execute(
        """
        update knowledge_field_values fv
           set value_text = t.id::text,
               target_node_id = null,
               updated_at = now()
          from knowledge_fields f, tasks t
         where fv.field_id = f.id
           and f.system_key = 'meeting_related_task'
           and t.knowledge_node_id = fv.node_id
           and (fv.target_node_id = fv.node_id or fv.value_text is distinct from t.id::text)
        """
    )
    return {"meeting_related_task_values_fixed": cur.rowcount}


MARKDOWN_LINK_RE = re.compile(r"^\[([^\]]+)\]\((?:https?://|mailto:)[^)]+\)$")


def cleanup_markdown_titles(cur) -> dict[str, int]:
    cur.execute("select id, title from knowledge_nodes where title like '[%](%)' and archived_at is null")
    rows = fetchall(cur)
    changed = 0
    for row in rows:
        match = MARKDOWN_LINK_RE.match(row["title"])
        if not match:
            continue
        cur.execute("update knowledge_nodes set title = %s, updated_at = now() where id = %s", (match.group(1), row["id"]))
        cur.execute(
            "update knowledge_search_index set title_text = %s where node_id = %s",
            (match.group(1), row["id"]),
        )
        changed += 1
    return {"markdown_titles_fixed": changed}


def get_fwd_project(cur) -> dict[str, Any]:
    cur.execute(
        """
        select id, name, knowledge_node_id
          from projects
         where deleted_at is null
           and knowledge_node_id is not null
           and (name ilike '%FWD%' or name ilike '%FW%')
         order by case when name ilike '%FWD%' then 0 else 1 end, created_at
         limit 1
        """
    )
    row = fetchone(cur)
    if not row:
        raise RuntimeError("FWD project with knowledge_node_id was not found")
    return row


def field_id(cur, workspace_id: str, system_key: str) -> str | None:
    cur.execute("select id from knowledge_fields where workspace_id = %s and system_key = %s limit 1", (workspace_id, system_key))
    row = fetchone(cur)
    return str(row["id"]) if row else None


def upsert_field_value(cur, node_id: str, field_id_value: str, *, value_text: str | None = None, value_number: float | None = None, value_datetime: str | None = None, target_node_id: str | None = None, updated_by: str | None = None) -> None:
    cur.execute(
        """
        insert into knowledge_field_values
          (node_id, field_id, value_json, value_text, value_number, value_datetime, target_node_id, updated_by)
        values (%s, %s, null, %s, %s, %s, %s, %s)
        on conflict (node_id, field_id) do update
          set value_text = excluded.value_text,
              value_number = excluded.value_number,
              value_datetime = excluded.value_datetime,
              target_node_id = excluded.target_node_id,
              updated_at = now(),
              updated_by = excluded.updated_by
        """,
        (node_id, field_id_value, value_text, value_number, value_datetime, target_node_id, updated_by),
    )


def ensure_procedure_wiki(cur, workspace_id: str, data_key: bytes, procedure_tag_id: str) -> dict[str, Any]:
    project = get_fwd_project(cur)
    root_id = project["knowledge_node_id"]
    cur.execute("select root_page_id, created_by, updated_by from knowledge_nodes where id = %s", (root_id,))
    root = fetchone(cur)
    section_id, section_created = ensure_node(
        cur,
        workspace_id=workspace_id,
        parent_id=root_id,
        root_page_id=root["root_page_id"] or root_id,
        project_id=project["id"],
        title=PROCEDURE_SECTION_TITLE,
        body="常に最新が正となる手順・ルールを置く。",
        data_key=data_key,
        created_by=root["created_by"],
        updated_by=root["updated_by"],
        body_json={"format": "doc_block", "block_type": "heading_2"},
        sort_order=30,
    )
    created_pages: list[str] = []
    for index, page in enumerate(PROCEDURE_PAGES):
        page_id, created = ensure_node(
            cur,
            workspace_id=workspace_id,
            parent_id=section_id,
            root_page_id=root["root_page_id"] or root_id,
            project_id=project["id"],
            title=page["title"],
            body="\n".join(page["lines"]),
            data_key=data_key,
            created_by=root["created_by"],
            updated_by=root["updated_by"],
            body_json={"format": "doc_block", "block_type": "heading_3"},
            sort_order=index,
            aliases=page["aliases"],
        )
        attach_tag(cur, page_id, procedure_tag_id, root["created_by"])
        for child_index, line in enumerate(page["lines"]):
            ensure_node(
                cur,
                workspace_id=workspace_id,
                parent_id=page_id,
                root_page_id=root["root_page_id"] or root_id,
                project_id=project["id"],
                title=line,
                body=line,
                data_key=data_key,
                created_by=root["created_by"],
                updated_by=root["updated_by"],
                body_json={"format": "doc_block", "block_type": "paragraph"},
                sort_order=child_index,
            )
        if created:
            created_pages.append(page["title"])
    return {"section_created": section_created, "procedure_pages_created": created_pages, "procedure_pages_expected": len(PROCEDURE_PAGES)}


def ensure_fw_tag_data(cur, workspace_id: str, data_key: bytes, tag_ids: dict[str, str]) -> dict[str, Any]:
    project = get_fwd_project(cur)
    root_id = project["knowledge_node_id"]
    cur.execute("select root_page_id, created_by, updated_by from knowledge_nodes where id = %s", (root_id,))
    root = fetchone(cur)
    root_page_id = root["root_page_id"] or root_id
    device_section_id, _ = ensure_node(
        cur,
        workspace_id=workspace_id,
        parent_id=root_id,
        root_page_id=root_page_id,
        project_id=project["id"],
        title="機器台帳",
        body="FW案件で扱う機器と保守情報。",
        data_key=data_key,
        created_by=root["created_by"],
        updated_by=root["updated_by"],
        body_json={"format": "doc_block", "block_type": "heading_2"},
        sort_order=35,
    )
    fields = {
        "model": field_id(cur, workspace_id, "device_model"),
        "quantity": field_id(cur, workspace_id, "device_quantity"),
        "usage": field_id(cur, workspace_id, "device_usage"),
        "location": field_id(cur, workspace_id, "device_location"),
        "asset": field_id(cur, workspace_id, "device_asset_number"),
        "maintenance": field_id(cur, workspace_id, "device_maintenance_due"),
    }
    created_devices = []
    for index, row in enumerate(DEVICE_ROWS):
        node_id, created = ensure_node(
            cur,
            workspace_id=workspace_id,
            parent_id=device_section_id,
            root_page_id=root_page_id,
            project_id=project["id"],
            title=row["title"],
            body=f"{row['usage']} / {row['location']}",
            data_key=data_key,
            created_by=root["created_by"],
            updated_by=root["updated_by"],
            body_json={"format": "doc_block", "block_type": "paragraph"},
            sort_order=index,
        )
        attach_tag(cur, node_id, tag_ids["device"], root["created_by"])
        if fields["model"]:
            upsert_field_value(cur, node_id, fields["model"], value_text=row["model"], updated_by=root["updated_by"])
        if fields["quantity"]:
            upsert_field_value(cur, node_id, fields["quantity"], value_number=float(row["quantity"]), updated_by=root["updated_by"])
        if fields["usage"]:
            upsert_field_value(cur, node_id, fields["usage"], value_text=row["usage"], updated_by=root["updated_by"])
        if fields["location"]:
            upsert_field_value(cur, node_id, fields["location"], value_text=row["location"], updated_by=root["updated_by"])
        if fields["asset"]:
            upsert_field_value(cur, node_id, fields["asset"], value_text=row["asset"], updated_by=root["updated_by"])
        if fields["maintenance"]:
            upsert_field_value(cur, node_id, fields["maintenance"], value_datetime=row["maintenance"], updated_by=root["updated_by"])
        if created:
            created_devices.append(row["title"])
    person_section_id, _ = ensure_node(
        cur,
        workspace_id=workspace_id,
        parent_id=root_id,
        root_page_id=root_page_id,
        project_id=project["id"],
        title="体制メモ",
        body="FW案件の連絡先と担当範囲。",
        data_key=data_key,
        created_by=root["created_by"],
        updated_by=root["updated_by"],
        body_json={"format": "doc_block", "block_type": "heading_2"},
        sort_order=36,
    )
    for index, title in enumerate(["運用窓口: 申請受付と進捗確認", "基盤担当: HUB/SUBA/DC1の変更調整"]):
        node_id, _ = ensure_node(
            cur,
            workspace_id=workspace_id,
            parent_id=person_section_id,
            root_page_id=root_page_id,
            project_id=project["id"],
            title=title,
            body=title,
            data_key=data_key,
            created_by=root["created_by"],
            updated_by=root["updated_by"],
            sort_order=index,
        )
        attach_tag(cur, node_id, tag_ids["person"], root["created_by"])
    decision_section_id, _ = ensure_node(
        cur,
        workspace_id=workspace_id,
        parent_id=root_id,
        root_page_id=root_page_id,
        project_id=project["id"],
        title="決定メモ",
        body="FW案件で確定した運用判断。",
        data_key=data_key,
        created_by=root["created_by"],
        updated_by=root["updated_by"],
        body_json={"format": "doc_block", "block_type": "heading_2"},
        sort_order=37,
    )
    for index, title in enumerate(["受付番号は26-9xxx体系で統一する", "緊急変更は通常申請と別枠で理由を残す"]):
        node_id, _ = ensure_node(
            cur,
            workspace_id=workspace_id,
            parent_id=decision_section_id,
            root_page_id=root_page_id,
            project_id=project["id"],
            title=title,
            body=title,
            data_key=data_key,
            created_by=root["created_by"],
            updated_by=root["updated_by"],
            sort_order=index,
        )
        attach_tag(cur, node_id, tag_ids["decision"], root["created_by"])
    return {"device_nodes_created": created_devices, "device_rows_expected": len(DEVICE_ROWS)}


def verification_counts(cur, workspace_id: str) -> dict[str, Any]:
    procedure_tag = fieldless_tag_id(cur, workspace_id, "procedure")
    cur.execute(
        """
        select count(*)::int as count
          from knowledge_node_supertags nst
         where nst.supertag_id = %s
        """,
        (procedure_tag,),
    )
    procedure_count = int(fetchone(cur)["count"]) if procedure_tag else 0
    cur.execute(
        """
        select count(*)::int as count
          from knowledge_supertags st
          left join knowledge_node_supertags nst on nst.supertag_id = st.id
         where st.workspace_id = %s
           and (lower(st.name) in ('risk', 'decision') or st.name like %s)
        """,
        (workspace_id, "%統合済み%"),
    )
    legacy_tags = int(fetchone(cur)["count"])
    cur.execute(
        """
        select count(*)::int as count
          from knowledge_nodes
         where archived_at is null
           and (title ilike '%Sample%' or title ilike '%Demo%')
        """
    )
    sample_words = int(fetchone(cur)["count"])
    return {"procedure_tagged_nodes": procedure_count, "legacy_english_or_merged_tags": legacy_tags, "sample_demo_title_nodes": sample_words}


def fieldless_tag_id(cur, workspace_id: str, system_key: str) -> str | None:
    cur.execute("select id from knowledge_supertags where workspace_id = %s and system_key = %s limit 1", (workspace_id, system_key))
    row = fetchone(cur)
    return str(row["id"]) if row else None


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env")
    data_key = load_data_key(repo_root)
    conn = connect()
    try:
        with conn:
            cursor_kwargs = {"row_factory": dict_row} if DB_KIND == "psycopg" else {"cursor_factory": RealDictCursor}
            with conn.cursor(**cursor_kwargs) as cur:
                cur.execute("select id from knowledge_workspaces order by created_at limit 1")
                workspace_row = fetchone(cur)
                if not workspace_row:
                    raise RuntimeError("knowledge_workspaces is empty")
                workspace_id = workspace_row["id"]
                tags_before = tag_snapshot(cur, workspace_id)
                schema = schema_status(cur)
                tags = ensure_cycle_tags(cur, workspace_id)
                tag_ids = {key: value for key, value in tags.items() if isinstance(value, str)}
                result = {
                    "ran_at": datetime.now(timezone.utc).isoformat(),
                    "schema_status": schema,
                    "tags_before": tags_before,
                    "tags": tags,
                    "task_canonical": migrate_task_canonical(cur, workspace_id, tag_ids["task"]),
                    "meeting_related_task": fix_meeting_related_task_values(cur),
                    "markdown_titles": cleanup_markdown_titles(cur),
                    "procedure_wiki": ensure_procedure_wiki(cur, workspace_id, data_key, tag_ids["procedure"]),
                    "fw_tag_data": ensure_fw_tag_data(cur, workspace_id, data_key, tag_ids),
                }
                result["tags_after"] = tag_snapshot(cur, workspace_id)
                result["verification"] = verification_counts(cur, workspace_id)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
