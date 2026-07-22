"""Project-scoped archival of mail attachments into Docs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from ..memory.models import (
    KnowledgeEdge,
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    KnowledgeSearchIndex,
    Project,
    Task,
    TaskReference,
)
from .docs_graph_service import DocsGraphService
from .project_information_docs import ensure_project_information_doc


MAIL_MANAGEMENT_TITLE = "メール管理"
MAIL_SUPERTAG_SYSTEM_KEY = "email"
_MESSAGE_ID_RE = re.compile(r"<([^<>]+)>")


@dataclass(frozen=True)
class ArchivedMail:
    node_id: UUID
    title: str
    created: bool
    dedupe_key: str


def normalize_message_id(value: Any) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split()).strip()
    match = _MESSAGE_ID_RE.search(text)
    return (match.group(1) if match else text.strip("<> ")).casefold()


def message_id_tokens(value: Any) -> list[str]:
    text = " ".join(str(value or "").replace("\x00", "").split()).strip()
    matches = _MESSAGE_ID_RE.findall(text)
    candidates = matches or text.split()
    return list(
        dict.fromkeys(
            normalize_message_id(candidate)
            for candidate in candidates
            if normalize_message_id(candidate)
        )
    )


def mail_dedupe_key(mail: dict[str, Any]) -> str:
    message_id = normalize_message_id(mail.get("message_id"))
    if message_id:
        return f"message-id:{message_id}"
    canonical = {
        "date": " ".join(str(mail.get("date") or "").split()),
        "from": " ".join(str(mail.get("sender") or "").casefold().split()),
        "subject": " ".join(str(mail.get("subject") or "").casefold().split()),
        "body": "\n".join(
            line.rstrip()
            for line in str(mail.get("body") or "")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .split("\n")
        ).strip(),
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return f"content-sha256:{digest}"


def _system_key(prefix: str, project_id: UUID, value: str = "") -> str:
    suffix = f":{hashlib.sha256(value.encode('utf-8')).hexdigest()}" if value else ""
    return f"{prefix}:{project_id}{suffix}"


def _list_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _title_chunks(
    value: Any, *, max_length: int = 450, max_chunks: int = 4
) -> list[str]:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    chunks: list[str] = []
    truncated = False
    for line in text.split("\n"):
        line_chunks = ["（空行）"] if not line else [
            line[index : index + max_length]
            for index in range(0, len(line), max_length)
        ]
        for chunk in line_chunks:
            if len(chunks) >= max_chunks:
                truncated = True
                break
            chunks.append(chunk)
        if truncated:
            break
    if truncated:
        chunks[-1] = "（続きはノードのフィールドに全文保存されています）"
    return chunks or ["（空）"]


class MailDocsService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.docs = DocsGraphService(session)

    async def archive_many(
        self,
        *,
        user_id: UUID,
        project_id: UUID,
        task_id: UUID | None = None,
        mails: list[dict[str, Any]],
    ) -> list[ArchivedMail]:
        if not mails:
            return []
        project = await self.session.scalar(
            select(Project)
            .where(Project.id == project_id, Project.deleted_at.is_(None))
            .with_for_update()
        )
        if project is None:
            raise ValueError("メール保存先プロジェクトが見つかりません。")
        if task_id is not None:
            task_exists = await self.session.scalar(
                select(Task.id).where(
                    Task.id == task_id,
                    Task.project_id == project_id,
                    Task.deleted_at.is_(None),
                )
            )
            if task_exists is None:
                raise ValueError("メール参照を追加する対象タスクが見つかりません。")
        workspace = await self.docs.ensure_workspace(user_id)
        project_information = await ensure_project_information_doc(
            self.session,
            project=project,
            user_id=user_id,
        )
        management = await self._ensure_management_node(
            workspace_id=workspace.id,
            project_id=project_id,
            user_id=user_id,
            parent=project_information,
        )
        archived: list[ArchivedMail] = []
        for mail in mails:
            result = await self._archive_one(
                workspace_id=workspace.id,
                management=management,
                project_id=project_id,
                user_id=user_id,
                mail=mail,
            )
            if task_id is not None:
                await self._ensure_task_reference(
                    task_id=task_id,
                    project_id=project_id,
                    user_id=user_id,
                    archived=result,
                )
            archived.append(result)
        await self.session.commit()
        return archived

    async def _find_system_node(
        self, *, workspace_id: UUID, project_id: UUID, system_key: str
    ) -> KnowledgeNode | None:
        result = await self.session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.workspace_id == workspace_id,
                KnowledgeNode.project_id == project_id,
                KnowledgeNode.system_key == system_key,
            )
        )
        return result.scalar_one_or_none()

    async def _create_unique_node(self, **kwargs: Any) -> tuple[KnowledgeNode, bool]:
        existing = await self._find_system_node(
            workspace_id=kwargs["workspace_id"],
            project_id=kwargs["project_id"],
            system_key=kwargs["system_key"],
        )
        if existing is not None:
            placement_changed = False
            if kwargs.get("parent") is not None:
                expected_parent_id = kwargs["parent"].id
                expected_root_id = kwargs["parent"].root_page_id or expected_parent_id
                placement_changed = (
                    existing.parent_id != expected_parent_id
                    or existing.root_page_id != expected_root_id
                )
                existing.parent_id = expected_parent_id
                existing.root_page_id = expected_root_id
            if existing.archived_at is not None:
                existing.archived_at = None
                existing.updated_by = kwargs["user_id"]
                await self.docs.record_node_change(
                    existing,
                    kwargs["user_id"],
                    "メール再登録によりノードを復元",
                )
                await self.session.flush()
            elif placement_changed:
                existing.updated_by = kwargs["user_id"]
                await self.docs.record_node_change(
                    existing,
                    kwargs["user_id"],
                    "メール管理ノードを案件情報配下へ修復",
                )
                await self.session.flush()
            return existing, False
        try:
            async with self.session.begin_nested():
                node = await self.docs.create_node(**kwargs)
            return node, True
        except IntegrityError:
            existing = await self._find_system_node(
                workspace_id=kwargs["workspace_id"],
                project_id=kwargs["project_id"],
                system_key=kwargs["system_key"],
            )
            if existing is None:
                raise
            return existing, False

    async def _ensure_management_node(
        self,
        *,
        workspace_id: UUID,
        project_id: UUID,
        user_id: UUID,
        parent: KnowledgeNode,
    ) -> KnowledgeNode:
        node, _ = await self._create_unique_node(
            workspace_id=workspace_id,
            user_id=user_id,
            title=MAIL_MANAGEMENT_TITLE,
            parent=parent,
            project_id=project_id,
            system_key=_system_key("project_mail_management", project_id),
            body_json={"format": "mail_management", "project_id": str(project_id)},
        )
        return node

    async def _archive_one(
        self,
        *,
        workspace_id: UUID,
        management: KnowledgeNode,
        project_id: UUID,
        user_id: UUID,
        mail: dict[str, Any],
    ) -> ArchivedMail:
        dedupe_key = mail_dedupe_key(mail)
        title = (
            str(mail.get("subject") or "（件名なし）").strip()[:500] or "（件名なし）"
        )
        node, created = await self._create_unique_node(
            workspace_id=workspace_id,
            user_id=user_id,
            title=title,
            parent=management,
            project_id=project_id,
            system_key=_system_key("project_mail", project_id, dedupe_key),
            body_json={"format": "email", "dedupe_key": dedupe_key},
            source_refs=[
                {"type": "workspace_file", "path": str(mail.get("source_path") or "")}
            ],
        )
        archived = ArchivedMail(node.id, node.title, created, dedupe_key)
        if not created:
            return archived

        tag = await self.docs.resolve_supertag(
            workspace_id=workspace_id,
            tag=MAIL_SUPERTAG_SYSTEM_KEY,
            create=False,
        )
        await self.docs.add_tag(node=node, tag=tag, user_id=user_id)
        fields = {
            "email_subject": str(mail.get("subject") or ""),
            "email_date": str(mail.get("date") or ""),
            "email_from": str(mail.get("sender") or ""),
            "email_to": _list_text(mail.get("to")),
            "email_cc": _list_text(mail.get("cc")),
            "email_bcc": _list_text(mail.get("bcc")),
            "email_message_id": str(mail.get("message_id") or ""),
            "email_in_reply_to": str(mail.get("in_reply_to") or ""),
            "email_references": _list_text(mail.get("references")),
            "email_body": str(mail.get("body") or ""),
            "email_source_filename": str(mail.get("name") or ""),
            "email_source_path": str(mail.get("source_path") or ""),
            "email_dedupe_key": dedupe_key,
        }
        await self.docs.set_fields(
            node=node,
            values={
                key: value for key, value in fields.items() if value not in ("", None)
            },
            user_id=user_id,
        )
        await self.docs.upsert_search_index(node)
        search_index = await self.session.get(KnowledgeSearchIndex, node.id)
        if search_index is not None:
            search_index.body_text_plain = str(mail.get("body") or "")
        await self._create_mail_outline(node=node, user_id=user_id, mail=mail)
        await self._link_thread(
            node=node, user_id=user_id, project_id=project_id, mail=mail
        )
        return archived

    async def _create_mail_outline(
        self, *, node: KnowledgeNode, user_id: UUID, mail: dict[str, Any]
    ) -> None:
        attributes = [
            ("件名", mail.get("subject")),
            ("メール日時", mail.get("date")),
            ("From", mail.get("sender")),
            ("To", _list_text(mail.get("to"))),
            ("CC", _list_text(mail.get("cc"))),
            ("BCC", _list_text(mail.get("bcc"))),
            ("Message-ID", mail.get("message_id")),
            ("In-Reply-To", mail.get("in_reply_to")),
            ("References", _list_text(mail.get("references"))),
            ("元ファイル名", mail.get("name")),
            ("元ファイルのプロジェクト内パス", mail.get("source_path")),
            ("本文", mail.get("body")),
        ]
        for label, value in attributes:
            label_node = await self.docs.create_node(
                workspace_id=node.workspace_id,
                user_id=user_id,
                title=label,
                parent=node,
                project_id=node.project_id,
            )
            for chunk in _title_chunks(
                value, max_chunks=32 if label == "本文" else 4
            ):
                await self.docs.create_node(
                    workspace_id=node.workspace_id,
                    user_id=user_id,
                    title=chunk,
                    parent=label_node,
                    project_id=node.project_id,
                )

    async def _nodes_by_message_ids(
        self, *, project_id: UUID, message_ids: list[str]
    ) -> dict[str, KnowledgeNode]:
        if not message_ids:
            return {}
        result = await self.session.execute(
            select(KnowledgeNode, KnowledgeFieldValue.value_text)
            .join(KnowledgeFieldValue, KnowledgeFieldValue.node_id == KnowledgeNode.id)
            .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
            .where(
                KnowledgeNode.project_id == project_id,
                KnowledgeNode.archived_at.is_(None),
                KnowledgeField.system_key == "email_message_id",
            )
        )
        wanted = set(message_ids)
        return {
            normalized: node
            for node, value in result.all()
            if (normalized := normalize_message_id(value)) in wanted
        }

    async def _ensure_edge(
        self,
        *,
        source: KnowledgeNode,
        target: KnowledgeNode,
        relation_type: str,
        user_id: UUID,
    ) -> None:
        if source.id == target.id or source.project_id != target.project_id:
            return
        result = await self.session.execute(
            select(KnowledgeEdge.id).where(
                KnowledgeEdge.source_node_id == source.id,
                KnowledgeEdge.target_node_id == target.id,
                KnowledgeEdge.relation_type == relation_type,
            )
        )
        if result.scalar_one_or_none() is None:
            self.session.add(
                KnowledgeEdge(
                    source_node_id=source.id,
                    target_node_id=target.id,
                    relation_type=relation_type,
                    confidence=1,
                    created_by=user_id,
                )
            )

    async def _link_thread(
        self,
        *,
        node: KnowledgeNode,
        user_id: UUID,
        project_id: UUID,
        mail: dict[str, Any],
    ) -> None:
        reply_ids = message_id_tokens(mail.get("in_reply_to"))
        reference_ids = message_id_tokens(mail.get("references"))
        targets = await self._nodes_by_message_ids(
            project_id=project_id,
            message_ids=list(dict.fromkeys([*reply_ids, *reference_ids])),
        )
        for message_id in reply_ids:
            if target := targets.get(message_id):
                await self._ensure_edge(
                    source=node,
                    target=target,
                    relation_type="email_reply_to",
                    user_id=user_id,
                )
        for message_id in reference_ids:
            if target := targets.get(message_id):
                await self._ensure_edge(
                    source=node,
                    target=target,
                    relation_type="email_thread_reference",
                    user_id=user_id,
                )

        current_id = normalize_message_id(mail.get("message_id"))
        if not current_id:
            return
        result = await self.session.execute(
            select(
                KnowledgeNode, KnowledgeField.system_key, KnowledgeFieldValue.value_text
            )
            .join(KnowledgeFieldValue, KnowledgeFieldValue.node_id == KnowledgeNode.id)
            .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
            .where(
                KnowledgeNode.project_id == project_id,
                KnowledgeNode.id != node.id,
                KnowledgeNode.archived_at.is_(None),
                KnowledgeField.system_key.in_(
                    ["email_in_reply_to", "email_references"]
                ),
                or_(
                    KnowledgeFieldValue.value_text.ilike(f"%{current_id}%"),
                    KnowledgeFieldValue.value_text.ilike(f"%<{current_id}>%"),
                ),
            )
        )
        for referencing_node, system_key, value in result.all():
            if current_id not in message_id_tokens(value):
                continue
            await self._ensure_edge(
                source=referencing_node,
                target=node,
                relation_type=(
                    "email_reply_to"
                    if system_key == "email_in_reply_to"
                    else "email_thread_reference"
                ),
                user_id=user_id,
            )

    async def _ensure_task_reference(
        self,
        *,
        task_id: UUID,
        project_id: UUID,
        user_id: UUID,
        archived: ArchivedMail,
    ) -> None:
        dedupe_key = f"{archived.node_id}||"
        result = await self.session.execute(
            select(TaskReference).where(
                TaskReference.task_id == task_id,
                TaskReference.reference_type == "docs_node",
                TaskReference.relation_type == "source",
                TaskReference.dedupe_key == dedupe_key,
            )
        )
        if result.scalar_one_or_none() is not None:
            return
        self.session.add(
            TaskReference(
                task_id=task_id,
                project_id=project_id,
                reference_type="docs_node",
                relation_type="source",
                target_id=str(archived.node_id),
                display_name=archived.title,
                dedupe_key=dedupe_key,
                reference_metadata={
                    "source": "work_intake_mail",
                    "mail_dedupe_key": archived.dedupe_key,
                },
                created_by=user_id,
            )
        )
