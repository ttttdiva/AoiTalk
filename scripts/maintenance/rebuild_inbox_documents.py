"""Re-synthesize existing Inbox documents from stored mail sources.

Dry-run is the default.  ``--apply`` stores a JSON backup under ``artifacts/``,
creates addressable message-source nodes, and replaces only system-generated
Inbox document roots.  User-created children are preserved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.config import Config
from src.memory.database import get_database_manager
from src.memory.models import (
    KnowledgeEdge,
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    Task,
    TaskReference,
)
from src.services.docs_graph_service import DocsGraphService
from src.services.inbox_document_service import (
    InboxSourceMaterial,
    build_inbox_document_prompt,
    parse_inbox_document,
)
from src.services.mail_docs_service import MailDocsService
from src.services.work_intake_docs_service import WorkIntakeDocsService


LOCK_KEY = 0x414F49494E424F58


async def _field_map(session, node_id: UUID) -> dict[str, str]:
    result = await session.execute(
        select(KnowledgeField.system_key, KnowledgeFieldValue.value_text)
        .join(KnowledgeFieldValue, KnowledgeFieldValue.field_id == KnowledgeField.id)
        .where(KnowledgeFieldValue.node_id == node_id)
    )
    return {
        str(key): str(value or "")
        for key, value in result.all()
        if key
    }


async def _source_mail_nodes(
    session,
    *,
    item: KnowledgeNode,
    project_nodes: list[KnowledgeNode],
) -> list[KnowledgeNode]:
    by_parent: dict[UUID | None, list[KnowledgeNode]] = {}
    for node in project_nodes:
        by_parent.setdefault(node.parent_id, []).append(node)
    descendants: set[UUID] = {item.id}
    queue = [item.id]
    while queue:
        parent_id = queue.pop()
        for child in by_parent.get(parent_id, []):
            if child.id not in descendants:
                descendants.add(child.id)
                queue.append(child.id)
    edge_result = await session.execute(
        select(KnowledgeEdge.target_node_id).where(
            KnowledgeEdge.source_node_id.in_(descendants),
            KnowledgeEdge.relation_type == "inline_ref",
        )
    )
    candidate_ids = set(edge_result.scalars().all())
    derived_result = await session.execute(
        select(KnowledgeEdge.target_node_id).where(
            KnowledgeEdge.source_node_id.in_(candidate_ids),
            KnowledgeEdge.relation_type == "derived_from_email",
        )
    )
    candidate_ids.update(derived_result.scalars().all())

    task_ids = set(
        (
            await session.execute(
                select(Task.id).where(
                    Task.knowledge_node_id == item.id,
                    Task.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    )
    if task_ids:
        ref_result = await session.execute(
            select(TaskReference.target_id).where(
                TaskReference.task_id.in_(task_ids),
                TaskReference.reference_type == "docs_node",
                TaskReference.relation_type == "source",
            )
        )
        for value in ref_result.scalars().all():
            try:
                candidate_ids.add(UUID(str(value)))
            except (TypeError, ValueError):
                pass
    return [
        node
        for node in project_nodes
        if node.id in candidate_ids
        and str(node.system_key or "").startswith("project_mail:")
        and not str(node.system_key or "").startswith("project_mail_management:")
    ]


def _mail_from_fields(fields: dict[str, str]) -> dict[str, Any]:
    return {
        "subject": fields.get("email_subject", ""),
        "date": fields.get("email_date", ""),
        "sender": fields.get("email_from", ""),
        "to": fields.get("email_to", ""),
        "cc": fields.get("email_cc", ""),
        "bcc": fields.get("email_bcc", ""),
        "message_id": fields.get("email_message_id", ""),
        "in_reply_to": fields.get("email_in_reply_to", ""),
        "references": fields.get("email_references", ""),
        "body": fields.get("email_body", ""),
        "name": fields.get("email_source_filename", ""),
        "source_path": fields.get("email_source_path", ""),
    }


async def _snapshot(session, items: list[KnowledgeNode]) -> dict[str, Any]:
    item_ids = {item.id for item in items}
    all_nodes = list(
        (
            await session.execute(select(KnowledgeNode))
        ).scalars().all()
    )
    by_parent: dict[UUID | None, list[KnowledgeNode]] = {}
    for node in all_nodes:
        by_parent.setdefault(node.parent_id, []).append(node)
    captured: list[dict[str, Any]] = []
    queue = list(item_ids)
    seen: set[UUID] = set()
    while queue:
        node_id = queue.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        node = next((candidate for candidate in all_nodes if candidate.id == node_id), None)
        if node is None:
            continue
        captured.append(
            {
                "id": str(node.id),
                "parent_id": str(node.parent_id) if node.parent_id else None,
                "project_id": str(node.project_id) if node.project_id else None,
                "system_key": node.system_key,
                "title": node.title,
                "body_json": node.body_json,
                "display_props": node.display_props,
                "archived_at": node.archived_at.isoformat() if node.archived_at else None,
                "fields": await _field_map(session, node.id),
            }
        )
        queue.extend(child.id for child in by_parent.get(node.id, []))
    return {"created_at": datetime.now().isoformat(), "nodes": captured}


async def _generate(prompt: str) -> str:
    def run() -> str:
        from src.llm.manager import create_llm_client

        return str(create_llm_client(Config()).generate_response(prompt, stream=False))

    return await asyncio.to_thread(run)


async def run(*, apply: bool, item_ids: set[UUID] | None) -> int:
    session = await get_database_manager().get_session()
    lock_acquired = False
    try:
        await session.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
        lock_acquired = True
        query = select(KnowledgeNode).where(
            KnowledgeNode.archived_at.is_(None),
            KnowledgeNode.system_key.like("project_inbox_item:%"),
        )
        if item_ids:
            query = query.where(KnowledgeNode.id.in_(item_ids))
        items = [
            item
            for item in (await session.execute(query)).scalars().all()
            if len(str(item.system_key or "").split(":")) == 3
        ]
        print(f"対象Inbox項目: {len(items)}件")
        if not items:
            return 0
        if apply:
            backup = await _snapshot(session, items)
            backup_dir = ROOT_DIR / "artifacts" / "inbox-rebuild"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"backup-{datetime.now():%Y%m%d-%H%M%S}.json"
            backup_path.write_text(
                json.dumps(backup, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            print(f"backup: {backup_path}")

        rebuilt = 0
        for item in items:
            project_nodes = list(
                (
                    await session.execute(
                        select(KnowledgeNode).where(
                            KnowledgeNode.project_id == item.project_id,
                            KnowledgeNode.archived_at.is_(None),
                        )
                    )
                ).scalars().all()
            )
            raw_nodes = await _source_mail_nodes(
                session,
                item=item,
                project_nodes=project_nodes,
            )
            print(f"- {item.id} {item.title}: 原本メール {len(raw_nodes)}件")
            if not raw_nodes:
                print("  skip: 根拠メールを特定できません")
                continue
            fields = await _field_map(session, item.id)
            mails = [_mail_from_fields(await _field_map(session, node.id)) for node in raw_nodes]
            intake_docs = WorkIntakeDocsService(session)
            expected_revision = await intake_docs.document_revision(item)
            checkpoint = await session.begin_nested()
            if apply:
                try:
                    archived = await MailDocsService(session).archive_many(
                        user_id=item.updated_by or item.created_by,
                        project_id=item.project_id,
                        mails=mails,
                        commit=False,
                    )
                except Exception:
                    await checkpoint.rollback()
                    raise
            else:
                await checkpoint.rollback()
                from src.services.mail_thread_parser import split_mail_thread

                archived = []
                print(
                    "  messages:",
                    sum(len(split_mail_thread(mail)) for mail in mails),
                )
                continue
            materials = [
                InboxSourceMaterial(
                    key=message.source_key,
                    node_id=message.node_id,
                    title=message.title,
                    date=message.date,
                    sender=message.sender,
                    content=message.body,
                    kind="email_message",
                )
                for raw in archived
                for message in raw.messages
            ]
            prompt = build_inbox_document_prompt(
                instruction=fields.get("inbox_instruction", ""),
                sources=materials,
                current_document=fields.get("inbox_summary", ""),
            )
            try:
                document = parse_inbox_document(
                    await _generate(prompt),
                    allowed_source_keys=[material.key for material in materials],
                )
            except Exception as exc:
                await checkpoint.rollback()
                print(f"  skip: 文書生成失敗: {exc}")
                continue
            try:
                await intake_docs.replace_document(
                    item_id=item.id,
                    project_id=item.project_id,
                    user_id=item.updated_by or item.created_by,
                    document=document,
                    source_nodes={
                        material.key: material.node_id
                        for material in materials
                        if material.node_id is not None
                    },
                    expected_revision=expected_revision,
                )
                await checkpoint.commit()
            except Exception as exc:
                await checkpoint.rollback()
                print(f"  skip: 文書保存失敗: {exc}")
                continue
            await session.commit()
            rebuilt += 1
            print(f"  rebuilt: {document.title} ({len(materials)} messages)")
        print(f"再構築完了: {rebuilt}/{len(items)}件")
        return 0 if (not apply or rebuilt == len(items)) else 2
    except Exception:
        await session.rollback()
        raise
    finally:
        if lock_acquired:
            await session.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": LOCK_KEY},
            )
        await session.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--item-id", action="append", default=[])
    args = parser.parse_args()
    return asyncio.run(
        run(
            apply=args.apply,
            item_ids={UUID(value) for value in args.item_id} or None,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
