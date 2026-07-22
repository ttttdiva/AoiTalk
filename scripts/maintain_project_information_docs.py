"""Maintain project information Docs data for the Docs cycle 10 fixes.

Default execution performs all maintenance steps and prints a verification JSON:
  - reseed the FWD project information page as "FW申請 案件情報"
  - move flat SLCO project-information body nodes under their section headings
  - replace empty English project-information templates with the Japanese template

Run with the project venv:
  venv\\Scripts\\python.exe scripts\\maintain_project_information_docs.py
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    import psycopg  # type: ignore

    DB_KIND = "psycopg"
except ImportError:
    try:
        import psycopg2  # type: ignore
        from psycopg2.extras import Json  # type: ignore

        DB_KIND = "psycopg2"
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("psycopg or psycopg2 is required. Run with venv\\Scripts\\python.exe") from exc


NODE_BODY_TEXT_AAD = "knowledge_nodes.body_text"
NODE_BODY_JSON_AAD = "knowledge_nodes.body_json"
REVISION_BODY_TEXT_AAD = "knowledge_revisions.body_text"
REVISION_BODY_JSON_AAD = "knowledge_revisions.body_json"
ENCRYPTION_PREFIX = "enc:v1:"
SEED_MARKER = "fw_project_information_v1"
FW_ROOT_TITLE = "FW申請 案件情報"
JAPANESE_TEMPLATE = [
    ("概要", "この案件の目的、背景、現在の状態を書く。"),
    ("体制", "関係者、役割、連絡経路を書く。"),
    ("決定事項", "決定した内容、日付、根拠リンクを書く。"),
    ("課題", "未解決の論点、リスク、次の確認先を書く。"),
    ("タスク", "実行する作業、担当、期限を書く。"),
    ("参照", "根拠資料、会議メモ、関連ノードのリンクを書く。"),
]
ENGLISH_TEMPLATE_TITLES = ["Overview", "Scope", "Assumptions", "Decisions", "Issues", "References", "Q&A"]
EMPTY_TEMPLATE_BODIES = {"", "Not documented yet", "[[project-qa]]"}
SLCO_SECTION_TITLES = [
    "概要",
    "体制",
    "進捗",
    "課題管理",
    "決定事項",
    "タスク",
    "Q&A",
    "会議メモ",
    "確認事項",
    "参照",
    "会議",
    "リスク",
    "検索ノード",
]


@dataclass
class SeedNode:
    id: str
    parent_id: str | None
    title: str
    block_type: str
    sort_order: float
    supertag: str | None = None
    fields: dict[str, Any] | None = None
    query_json: dict[str, Any] | None = None


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="ignore")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def connection_kwargs() -> dict[str, Any] | str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    return {
        "host": os.environ.get("POSTGRES_HOST", "localhost"),
        "port": int(os.environ.get("POSTGRES_PORT", "5432")),
        "dbname": os.environ.get("POSTGRES_DB", "aoitalk_memory"),
        "user": os.environ.get("POSTGRES_USER", "postgres"),
        "password": os.environ.get("POSTGRES_PASSWORD", ""),
    }


def connect():
    kwargs = connection_kwargs()
    os.environ.setdefault("PGCLIENTENCODING", "UTF8")
    os.environ.setdefault("PGOPTIONS", "--client_encoding=UTF8")
    if DB_KIND == "psycopg":
        if isinstance(kwargs, str):
            return psycopg.connect(kwargs)  # type: ignore[name-defined]
        return psycopg.connect(**kwargs)  # type: ignore[name-defined]
    if isinstance(kwargs, str):
        return psycopg2.connect(kwargs)  # type: ignore[name-defined]
    return psycopg2.connect(**kwargs)  # type: ignore[name-defined]


def json_param(value: Any) -> Any:
    if value is None:
        return None
    if DB_KIND == "psycopg2":
        return Json(value)
    return value


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(padded)


def load_data_key(repo_root: Path) -> bytes:
    env_key = os.environ.get("AOITALK_FIELD_CRYPTO_KEY_B64")
    if env_key:
        if os.environ.get("AOITALK_FIELD_CRYPTO_ALLOW_ENV_KEY", "").lower() not in {"1", "true", "yes"}:
            raise SystemExit("AOITALK_FIELD_CRYPTO_KEY_B64 is set but env keys are disabled")
        key = base64.b64decode(env_key)
    else:
        script = repo_root / "scripts" / "field_crypto_key.ps1"
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Action",
                "GetOrCreateDataKey",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit(f"field crypto key command failed: {result.stderr.strip()}")
        key = base64.b64decode(result.stdout.strip().splitlines()[-1])
    if len(key) != 32:
        raise SystemExit("field crypto data key must be 32 bytes")
    return key


def encrypt_text(value: str, aad: str, data_key: bytes) -> str:
    if not value or value.startswith(ENCRYPTION_PREFIX):
        return value
    nonce = os.urandom(12)
    sealed = AESGCM(data_key).encrypt(nonce, value.encode("utf-8"), aad.encode("utf-8"))
    return f"enc:v1:aes256gcm:local:{b64url(nonce)}:{b64url(sealed)}"


def decrypt_text(value: str | None, aad: str, data_key: bytes) -> str:
    if not value:
        return ""
    if not value.startswith("enc:v1:aes256gcm:local:"):
        return value
    parts = value.split(":")
    if len(parts) != 7:
        return ""
    try:
        nonce = b64url_decode(parts[5])
        sealed = b64url_decode(parts[6])
        return AESGCM(data_key).decrypt(nonce, sealed, aad.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


def encrypt_json(value: dict[str, Any], aad: str, data_key: bytes) -> dict[str, Any] | str:
    if not value:
        return value
    return encrypt_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), aad, data_key)


def node(parent_id: str | None, title: str, block_type: str, sort_order: float, **kwargs: Any) -> SeedNode:
    return SeedNode(str(uuid.uuid4()), parent_id, title, block_type, sort_order, **kwargs)


def resolve_project(cur, env_name: str, name_pattern: str) -> dict[str, Any]:
    project_id = os.environ.get(env_name)
    if project_id:
        cur.execute("select id, name, owner_id, knowledge_node_id from projects where id = %s", (project_id,))
    else:
        cur.execute(
            """
            select id, name, owner_id, knowledge_node_id
              from projects
             where deleted_at is null and name ilike %s
             order by updated_at desc nulls last, created_at desc nulls last
             limit 1
            """,
            (name_pattern,),
        )
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"project not found for {env_name or name_pattern}")
    return {"id": row[0], "name": row[1], "owner_id": row[2], "knowledge_node_id": row[3]}


def resolve_workspace_id(cur, project_id: str, fallback: str | None = None) -> str:
    if fallback:
        return fallback
    cur.execute(
        """
        select workspace_id
          from knowledge_nodes
         where project_id = %s
         order by updated_at desc nulls last, created_at desc nulls last
         limit 1
        """,
        (project_id,),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("select id from knowledge_workspaces order by created_at nulls last limit 1")
    row = cur.fetchone()
    if not row:
        raise SystemExit("knowledge workspace not found")
    return row[0]


def ensure_supertag(cur, workspace_id: str, system_key: str, name: str, base_type: str, color: str, icon: str) -> str:
    cur.execute(
        "select id from knowledge_supertags where workspace_id = %s and system_key = %s",
        (workspace_id, system_key),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """
        select id
          from knowledge_supertags
         where workspace_id = %s and lower(name) = lower(%s)
         order by created_at nulls last
         limit 1
        """,
        (workspace_id, name),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            """
            update knowledge_supertags
               set system_key = %s, base_type = %s, color = coalesce(color, %s), icon = coalesce(icon, %s), updated_at = now()
             where id = %s
            """,
            (system_key, base_type, color, icon, row[0]),
        )
        return row[0]
    tag_id = str(uuid.uuid4())
    cur.execute(
        """
        insert into knowledge_supertags
          (id, workspace_id, system_key, name, base_type, color, icon, template_json, pinned_field_ids, config_json)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (tag_id, workspace_id, system_key, name, base_type, color, icon, json_param({}), json_param([]), json_param({})),
    )
    return tag_id


def ensure_field(
    cur,
    workspace_id: str,
    tag_id: str,
    system_key: str,
    name: str,
    field_type: str,
    sort_order: float,
    options: dict[str, Any] | None = None,
) -> str:
    cur.execute(
        "select id from knowledge_fields where workspace_id = %s and system_key = %s",
        (workspace_id, system_key),
    )
    row = cur.fetchone()
    if row:
        field_id = row[0]
    else:
        field_id = str(uuid.uuid4())
        cur.execute(
            """
            insert into knowledge_fields
              (id, workspace_id, supertag_id, system_key, name, field_type, options_json, sort_order)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (field_id, workspace_id, tag_id, system_key, name, field_type, json_param(options or {}), sort_order),
        )
    cur.execute(
        """
        insert into knowledge_supertag_fields (supertag_id, field_id, sort_order, required, show_in_template, optional)
        values (%s, %s, %s, false, true, true)
        on conflict (supertag_id, field_id) do update
           set sort_order = excluded.sort_order
        """,
        (tag_id, field_id, sort_order),
    )
    return field_id


def build_fw_nodes(project_id: str, task_status_field_id: str) -> list[SeedNode]:
    nodes: list[SeedNode] = []

    def add(parent_id: str | None, title: str, block_type: str, sort_order: float, **kwargs: Any) -> SeedNode:
        item = node(parent_id, title, block_type, sort_order, **kwargs)
        nodes.append(item)
        return item

    root = add(None, FW_ROOT_TITLE, "heading_1", 1)
    overview = add(root.id, "概要", "heading_1", 1)
    add(overview.id, "対象は申請番号26-9014、26-9021、26-9033の3件。いずれも架空の社内申請体系に基づく確認案件として扱う。", "paragraph", 1)
    add(overview.id, "目的は、申請前の差分説明、承認依頼先、期限超過時の扱いを案件ページ上で一本化すること。", "paragraph", 2)
    add(overview.id, "現在状態: 26-9014は提出前確認中、26-9021は差分根拠の追記待ち、26-9033は承認依頼先の確定待ち。", "paragraph", 3)

    team = add(root.id, "体制", "heading_2", 2)
    add(team.id, "申請調整: 案件窓口が申請番号ごとの論点と期限を管理する。", "paragraph", 1)
    add(team.id, "内容確認: 構成レビュー担当が差分説明と承認条件の整合を確認する。", "paragraph", 2)
    add(team.id, "承認連携: 承認依頼担当が回答待ちと期限超過時の扱いを記録する。", "paragraph", 3)

    decisions = add(root.id, "決定事項", "heading_2", 3)
    add(decisions.id, "2026-07-03: 26-9014を先行処理し、26-9021と26-9033は差分根拠がそろった順に提出する。根拠: [申請整理メモ](https://docs.example.invalid/fw-approval/minutes/2026-07-03)", "paragraph", 1)
    add(decisions.id, "2026-07-04: 期限超過の可能性が出た場合は、承認依頼担当が当日中に代替日と影響範囲を追記する。根拠: [期限確認ログ](https://docs.example.invalid/fw-approval/deadline-check)", "paragraph", 2)

    issues = add(root.id, "課題", "heading_2", 4)
    add(issues.id, "申請26-9021の差分説明が承認条件と一致していない", "paragraph", 1, supertag="risk")
    add(issues.id, "受付回答待ちが2営業日を超える場合、26-9033の承認依頼日が後ろ倒しになる", "paragraph", 2, supertag="risk")

    tasks_section = add(root.id, "タスク", "heading_2", 5)
    add(tasks_section.id, "申請26-9014の提出前チェックを完了する", "checkbox", 1, supertag="task", fields={"task_status": "todo", "task_due": "2026-07-09"})
    add(tasks_section.id, "申請26-9021の差分根拠を追記する", "checkbox", 2, supertag="task", fields={"task_status": "doing", "task_due": "2026-07-10"})
    add(tasks_section.id, "申請26-9033の承認依頼先を確定する", "checkbox", 3, supertag="task", fields={"task_status": "todo", "task_due": "2026-07-12"})

    meeting_section = add(root.id, "会議メモ", "heading_2", 6)
    meeting = add(meeting_section.id, "2026-07-03 申請整理会", "heading_3", 1, supertag="meeting")
    add(meeting.id, "経緯: 申請番号ごとに確認粒度がばらつき、口頭確認のまま残っていた。", "paragraph", 1)
    add(meeting.id, "決定: 申請番号、根拠リンク、宿題を案件情報ノード配下で管理する。", "paragraph", 2)
    add(meeting.id, "宿題: 26-9021の差分根拠を追記し、26-9033の承認依頼先を確定する。", "paragraph", 3)

    references = add(root.id, "参照", "heading_2", 7)
    add(references.id, "[申請整理メモ](https://docs.example.invalid/fw-approval/minutes/2026-07-03)", "paragraph", 1)
    add(references.id, "[期限確認ログ](https://docs.example.invalid/fw-approval/deadline-check)", "paragraph", 2)

    search_section = add(root.id, "検索ノード", "heading_2", 8)
    add(
        search_section.id,
        "未完了タスク",
        "search",
        1,
        query_json={
            "and": [
                {"tag_system_key": "task"},
                {"field_id": task_status_field_id, "op": "!=", "value": "done"},
            ],
            "project_id": project_id,
            "limit": 50,
        },
    )
    add(search_section.id, "課題検索", "search", 2, query_json={"and": [{"tag_system_key": "risk"}], "project_id": project_id, "limit": 50})
    add(search_section.id, "会議メモ検索", "search", 3, query_json={"and": [{"tag_system_key": "meeting"}], "project_id": project_id, "limit": 50})
    return nodes


def insert_node(
    cur,
    *,
    item: SeedNode,
    root_id: str,
    workspace_id: str,
    project_id: str,
    user_id: str,
    tag_ids: dict[str, str],
    field_ids: dict[str, str],
    data_key: bytes,
) -> None:
    body_json: dict[str, Any] = {"format": "doc_block", "block_type": "paragraph" if item.block_type == "search" else item.block_type}
    if item.block_type == "checkbox":
        body_json["checked"] = False
    display_props = {"seed": SEED_MARKER}
    if item.block_type == "checkbox":
        display_props.update({"show_checkbox": True, "checked": False})
    node_type = "search" if item.block_type == "search" else "node"
    cur.execute(
        """
        insert into knowledge_nodes
          (id, workspace_id, parent_id, root_page_id, project_id, title, body_json, body_text,
           node_type, display_props, query_json, view_json, sort_order, created_by, updated_by)
        values
          (%s, %s, %s, %s, %s, %s, %s, %s,
           %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            item.id,
            workspace_id,
            item.parent_id,
            root_id,
            project_id,
            item.title,
            json_param(encrypt_json(body_json, NODE_BODY_JSON_AAD, data_key)),
            encrypt_text(item.title, NODE_BODY_TEXT_AAD, data_key),
            node_type,
            json_param(display_props),
            json_param(item.query_json) if node_type == "search" else None,
            json_param({"view": "list"} if node_type == "search" else {}),
            item.sort_order,
            user_id,
            user_id,
        ),
    )
    cur.execute(
        """
        insert into knowledge_search_index (node_id, workspace_id, project_id, title_text, body_text_plain)
        values (%s, %s, %s, %s, %s)
        on conflict (node_id) do update
           set title_text = excluded.title_text,
               body_text_plain = excluded.body_text_plain,
               updated_at = now()
        """,
        (item.id, workspace_id, project_id, item.title, item.title),
    )
    cur.execute(
        """
        insert into knowledge_revisions (id, node_id, title, body_json, body_text, change_summary, created_by)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid.uuid4()),
            item.id,
            item.title,
            json_param(encrypt_json(body_json, REVISION_BODY_JSON_AAD, data_key)),
            encrypt_text(item.title, REVISION_BODY_TEXT_AAD, data_key),
            "FW案件情報を作成",
            user_id,
        ),
    )
    if item.supertag and tag_ids.get(item.supertag):
        cur.execute(
            """
            insert into knowledge_node_supertags (node_id, supertag_id, created_by)
            values (%s, %s, %s)
            on conflict do nothing
            """,
            (item.id, tag_ids[item.supertag], user_id),
        )
    for field_key, value in (item.fields or {}).items():
        if item.supertag == "task" and field_key.startswith("task_"):
            continue
        field_id = field_ids.get(field_key)
        if not field_id:
            continue
        value_datetime = None
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            value_datetime = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        cur.execute(
            """
            insert into knowledge_field_values
              (node_id, field_id, value_json, value_text, value_datetime, updated_by)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (node_id, field_id) do update
               set value_json = excluded.value_json,
                   value_text = excluded.value_text,
                   value_datetime = excluded.value_datetime,
                   updated_at = now(),
                   updated_by = excluded.updated_by
            """,
            (item.id, field_id, json_param(value), str(value), value_datetime, user_id),
        )
    if item.supertag == "task":
        cur.execute(
            """
            insert into tasks
              (id, project_id, knowledge_node_id, title, status, source, created_by, end_at, task_metadata)
            values (%s, %s, %s, %s, %s, 'docs_seed_fw_info', %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                project_id,
                item.id,
                item.title,
                (item.fields or {}).get("task_status", "todo"),
                user_id,
                (item.fields or {}).get("task_due"),
                json_param({"source": "docs_seed_fw_info", "seed": SEED_MARKER}),
            ),
        )


def seed_fw_project_information(cur, project: dict[str, Any], workspace_id: str, user_id: str, data_key: bytes) -> dict[str, Any]:
    task_tag_id = ensure_supertag(cur, workspace_id, "task", "タスク", "task", "#22c55e", "check-square")
    meeting_tag_id = ensure_supertag(cur, workspace_id, "meeting", "会議", "meeting", "#0ea5e9", "users")
    risk_tag_id = ensure_supertag(cur, workspace_id, "risk", "リスク", "risk", "#ef4444", "triangle-alert")
    tag_ids = {"task": task_tag_id, "meeting": meeting_tag_id, "risk": risk_tag_id}
    task_status_field_id = ensure_field(
        cur,
        workspace_id,
        task_tag_id,
        "task_status",
        "状態",
        "options",
        1,
        {"values": ["todo", "doing", "done"]},
    )
    task_due_field_id = ensure_field(cur, workspace_id, task_tag_id, "task_due", "期日", "date", 2)
    field_ids = {"task_status": task_status_field_id, "task_due": task_due_field_id}

    project_id = project["id"]
    cur.execute("delete from tasks where project_id = %s and source in ('docs_seed', 'docs_seed_fw_info')", (project_id,))
    cur.execute("update projects set knowledge_node_id = null, updated_at = now() where id = %s", (project_id,))
    cur.execute(
        """
        with roots as (
            select distinct coalesce(root_page_id, id) as id
              from knowledge_nodes
             where project_id = %s
               and (
                    title in ('FWD Docs Sample', 'FWD 案件情報', 'FW申請 案件情報')
                 or title like 'FWD 案件情報%%'
                 or title ~ 'FW申請対応 [0-9]{3}(,[0-9]{3})+'
                 or display_props::text like %s
               )
        )
        delete from knowledge_nodes
         where id in (select id from roots)
            or root_page_id in (select id from roots)
        """,
        (project_id, f"%{SEED_MARKER}%"),
    )

    nodes = build_fw_nodes(project_id, task_status_field_id)
    root_id = nodes[0].id
    for item in nodes:
        insert_node(
            cur,
            item=item,
            root_id=root_id,
            workspace_id=workspace_id,
            project_id=project_id,
            user_id=user_id,
            tag_ids=tag_ids,
            field_ids=field_ids,
            data_key=data_key,
        )
    cur.execute("update projects set knowledge_node_id = %s, updated_at = now() where id = %s", (root_id, project_id))
    return verify_fw_project_information(cur, project_id, root_id, data_key)


def verify_fw_project_information(cur, project_id: str, root_id: str | None, data_key: bytes) -> dict[str, Any]:
    if not root_id:
        cur.execute(
            "select id from knowledge_nodes where project_id = %s and title = %s and archived_at is null order by created_at desc limit 1",
            (project_id, FW_ROOT_TITLE),
        )
        row = cur.fetchone()
        root_id = row[0] if row else None
    if not root_id:
        return {"root_id": None, "ok": False}
    cur.execute(
        """
        select title, body_text
          from knowledge_nodes
         where (id = %s or root_page_id = %s) and archived_at is null
        """,
        (root_id, root_id),
    )
    sample_terms = 0
    for title, body_text in cur.fetchall():
        plain = decrypt_text(body_text, NODE_BODY_TEXT_AAD, data_key)
        if re.search(r"sample|demo", title or "", re.I) or re.search(r"sample|demo", plain or "", re.I):
            sample_terms += 1
    cur.execute(
        """
        select count(*)
          from (
            select title
              from knowledge_nodes
             where root_page_id = %s and archived_at is null
             group by title
            having count(*) > 1
          ) s
        """,
        (root_id,),
    )
    duplicate_titles = cur.fetchone()[0]
    cur.execute(
        "select count(*) from knowledge_nodes where project_id = %s and archived_at is null and title ~ '[0-9]{3},[0-9]{3}'",
        (project_id,),
    )
    multi_number_titles = cur.fetchone()[0]
    cur.execute("select knowledge_node_id from projects where id = %s", (project_id,))
    project_points_to_root = cur.fetchone()[0] == root_id
    cur.execute(
        """
        select count(*)
          from tasks
         where project_id = %s
           and source = 'docs_seed_fw_info'
           and knowledge_node_id is not null
           and deleted_at is null
        """,
        (project_id,),
    )
    linked_tasks = cur.fetchone()[0]
    cur.execute(
        "select count(*) from knowledge_nodes where parent_id = %s and archived_at is null",
        (root_id,),
    )
    root_direct_count = cur.fetchone()[0]
    cur.execute(
        """
        select title
          from knowledge_nodes
         where parent_id = %s and archived_at is null
         order by sort_order
        """,
        (root_id,),
    )
    direct_titles = [row[0] for row in cur.fetchall()]
    cur.execute(
        """
        select count(*)
          from knowledge_nodes
         where root_page_id = %s
           and node_type = 'search'
           and archived_at is null
           and coalesce(query_json->>'project_id', '') <> %s
        """,
        (root_id, project_id),
    )
    search_nodes_without_scope = cur.fetchone()[0]
    return {
        "root_id": root_id,
        "root_title": FW_ROOT_TITLE,
        "duplicate_titles": duplicate_titles,
        "multi_number_titles": multi_number_titles,
        "project_points_to_root": project_points_to_root,
        "linked_tasks": linked_tasks,
        "sample_or_demo_terms": sample_terms,
        "root_direct_count": root_direct_count,
        "root_direct_titles": direct_titles,
        "search_nodes_without_project_scope": search_nodes_without_scope,
        "ok": duplicate_titles == 0
        and multi_number_titles == 0
        and project_points_to_root
        and linked_tasks >= 3
        and sample_terms == 0
        and root_direct_count <= len(SLCO_SECTION_TITLES)
        and search_nodes_without_scope == 0,
    }


def tree_lines(cur, root_id: str) -> list[str]:
    cur.execute(
        """
        with recursive tree as (
          select id, parent_id, title, sort_order, 0 as depth, array[sort_order] as path
            from knowledge_nodes
           where id = %s and archived_at is null
          union all
          select child.id, child.parent_id, child.title, child.sort_order, tree.depth + 1, tree.path || child.sort_order
            from knowledge_nodes child
            join tree on child.parent_id = tree.id
           where child.archived_at is null
        )
        select repeat('  ', depth) || '- ' || title
          from tree
         order by path
        """,
        (root_id,),
    )
    return [row[0] for row in cur.fetchall()]


def restructure_slco(cur, project: dict[str, Any]) -> dict[str, Any]:
    root_id = project["knowledge_node_id"]
    if not root_id:
        return {"project": project["name"], "skipped": "knowledge_node_id is empty"}
    before = tree_lines(cur, root_id)
    cur.execute(
        """
        select id, title, sort_order, node_type
          from knowledge_nodes
         where parent_id = %s and archived_at is null
         order by sort_order, created_at
        """,
        (root_id,),
    )
    children = [{"id": row[0], "title": row[1], "sort_order": row[2], "node_type": row[3]} for row in cur.fetchall()]
    section_ids = {child["title"]: child["id"] for child in children if child["title"] in SLCO_SECTION_TITLES}
    next_sort: dict[str, float] = {}
    for section_id in section_ids.values():
        cur.execute("select coalesce(max(sort_order), 0) from knowledge_nodes where parent_id = %s", (section_id,))
        next_sort[section_id] = float(cur.fetchone()[0] or 0)
    current_section: str | None = None
    moved: list[dict[str, str]] = []
    for child in children:
        title = child["title"]
        if title in section_ids:
            current_section = section_ids[title]
            continue
        if child["node_type"] == "search":
            continue
        target = current_section
        if "Day capture" in title:
            target = section_ids.get("会議メモ") or current_section
        if not target or target == child["id"]:
            continue
        next_sort[target] = next_sort.get(target, 0) + 1
        cur.execute(
            "update knowledge_nodes set parent_id = %s, sort_order = %s where id = %s",
            (target, next_sort[target], child["id"]),
        )
        moved.append({"title": title, "to": next(key for key, value in section_ids.items() if value == target)})
    meeting_section_id = section_ids.get("会議メモ")
    if meeting_section_id:
        cur.execute(
            """
            select id, title, parent_id
              from knowledge_nodes
             where root_page_id = %s
               and archived_at is null
               and title like '%%Day capture%%'
               and parent_id <> %s
            """,
            (root_id, meeting_section_id),
        )
        for node_id, title, _parent_id in cur.fetchall():
            next_sort[meeting_section_id] = next_sort.get(meeting_section_id, 0) + 1
            cur.execute(
                "update knowledge_nodes set parent_id = %s, sort_order = %s where id = %s",
                (meeting_section_id, next_sort[meeting_section_id], node_id),
            )
            moved.append({"title": title, "to": "会議メモ"})
    after = tree_lines(cur, root_id)
    return {"project": project["name"], "root_id": root_id, "moved_count": len(moved), "moved": moved, "before_tree": before, "after_tree": after}


def replace_empty_templates(cur, projects: list[dict[str, Any]], data_key: bytes) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for project in projects:
        root_id = project["knowledge_node_id"]
        if not root_id:
            results.append({"project": project["name"], "skipped": "knowledge_node_id is empty"})
            continue
        cur.execute(
            """
            select id, title, body_text, sort_order
              from knowledge_nodes
             where parent_id = %s and archived_at is null
             order by sort_order, created_at
            """,
            (root_id,),
        )
        children = [{"id": row[0], "title": row[1], "body_text": row[2], "sort_order": row[3]} for row in cur.fetchall()]
        titles = [child["title"] for child in children]
        expected_titles = [title for title, _guide in JAPANESE_TEMPLATE]
        if titles[: len(expected_titles)] == expected_titles:
            for index, child in enumerate(children[: len(expected_titles)]):
                cur.execute(
                    """
                    update knowledge_nodes
                       set root_page_id = %s,
                           sort_order = %s,
                           updated_at = now()
                     where id = %s
                    """,
                    (root_id, index, child["id"]),
                )
            archived: list[str] = []
            for child in children[len(expected_titles):]:
                body = decrypt_text(child["body_text"], NODE_BODY_TEXT_AAD, data_key).strip()
                if body in EMPTY_TEMPLATE_BODIES:
                    cur.execute(
                        "update knowledge_nodes set archived_at = now(), updated_at = now() where id = %s",
                        (child["id"],),
                    )
                    archived.append(child["title"])
            results.append({
                "project": project["name"],
                "replaced": False,
                "repaired": True,
                "titles": expected_titles,
                "archived": archived,
            })
            continue
        if titles != ENGLISH_TEMPLATE_TITLES:
            results.append({"project": project["name"], "replaced": False, "reason": "not_empty_english_template", "titles": titles})
            continue
        cur.execute(
            """
            select count(*)
              from knowledge_nodes
             where archived_at is null and parent_id = any(%s::uuid[])
            """,
            ([child["id"] for child in children],),
        )
        if cur.fetchone()[0] != 0:
            results.append({"project": project["name"], "replaced": False, "reason": "template_has_children"})
            continue
        bodies = [decrypt_text(child["body_text"], NODE_BODY_TEXT_AAD, data_key).strip() for child in children]
        if any(body not in EMPTY_TEMPLATE_BODIES for body in bodies):
            results.append({"project": project["name"], "replaced": False, "reason": "body_text_is_not_empty", "bodies": bodies})
            continue
        for index, (title, guide) in enumerate(JAPANESE_TEMPLATE):
            child = children[index]
            body_json = {"format": "project_information_section", "title": title}
            cur.execute(
                """
                update knowledge_nodes
                   set title = %s,
                       body_text = %s,
                       body_json = %s,
                       root_page_id = %s,
                       sort_order = %s,
                       updated_at = now()
                 where id = %s
                """,
                (
                    title,
                    encrypt_text(guide, NODE_BODY_TEXT_AAD, data_key),
                    json_param(encrypt_json(body_json, NODE_BODY_JSON_AAD, data_key)),
                    root_id,
                    index,
                    child["id"],
                ),
            )
            cur.execute(
                """
                insert into knowledge_search_index (node_id, workspace_id, project_id, title_text, body_text_plain)
                select id, workspace_id, project_id, title, %s
                  from knowledge_nodes
                 where id = %s
                on conflict (node_id) do update
                   set title_text = excluded.title_text,
                       body_text_plain = excluded.body_text_plain,
                       updated_at = now()
                """,
                (guide, child["id"]),
            )
        for child in children[len(JAPANESE_TEMPLATE):]:
            cur.execute("update knowledge_nodes set archived_at = now(), updated_at = now() where id = %s", (child["id"],))
        results.append({"project": project["name"], "replaced": True, "titles": [title for title, _ in JAPANESE_TEMPLATE]})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    load_dotenv(repo_root / ".env")
    data_key = load_data_key(repo_root)
    result: dict[str, Any] = {}
    with connect() as conn:
        cur = conn.cursor()
        fwd_project = resolve_project(cur, "FWD_PROJECT_ID", "%FWD%")
        slco_project = resolve_project(cur, "SLCO_PROJECT_ID", "%SLCO%")
        self_llm_project = resolve_project(cur, "SELF_LLM_PROJECT_ID", "%自社LLM%")
        aoitalk_project = resolve_project(cur, "AOITALK_PROJECT_ID", "%AoiTalk%")
        workspace_id = resolve_workspace_id(cur, fwd_project["id"], os.environ.get("FWD_WORKSPACE_ID"))
        user_id = os.environ.get("FWD_USER_ID") or fwd_project["owner_id"]
        if not user_id:
            raise SystemExit("user id could not be resolved")
        if args.verify_only:
            result["fw"] = verify_fw_project_information(cur, fwd_project["id"], fwd_project["knowledge_node_id"], data_key)
        else:
            result["fw"] = seed_fw_project_information(cur, fwd_project, workspace_id, user_id, data_key)
            result["slco"] = restructure_slco(cur, slco_project)
            result["template_replacements"] = replace_empty_templates(cur, [self_llm_project, aoitalk_project], data_key)
            conn.commit()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result.get("fw", {}).get("ok") is False:
        raise SystemExit("FW verification failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
