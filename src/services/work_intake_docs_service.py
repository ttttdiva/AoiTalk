"""Project-scoped Docs records for one /inbox reception."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..memory.models import (
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    DocsLibrary,
    Project,
    Task,
    User,
)
from .docs_graph_service import DocsGraphService
from .docs_acl import accessible_project_ids, can_write_node
from .inbox_item_resolution import (
    InboxSearchCandidate,
    RankedInboxCandidate,
    rank_inbox_candidates,
)
from .inbox_document_service import InboxDocument, InboxDocumentBlock
from .project_information_docs import (
    ensure_project_information_doc,
    is_default_inbox_project,
)


INBOX_MANAGEMENT_TITLE = "Inbox"
INBOX_SUPERTAG_SYSTEM_KEY = "work_intake"
INBOX_ITEM_SYSTEM_PREFIX = "project_inbox_item"
INBOX_MANAGEMENT_SYSTEM_PREFIX = "project_inbox_management"
INBOX_STATUS_VALUES = {
    "受付",
    "対応中",
    "確認待ち",
    "レビュー待ち",
    "完了",
    "保存のみ",
}
INBOX_UPDATE_TEXT_LIMIT = 131_072
INBOX_GENERATED_NODE_LIMIT = 450

_CLASSIFICATION_LABELS = {
    "question": "質問",
    "request": "依頼",
    "information_share": "情報共有",
    "質問": "質問",
    "依頼": "依頼",
    "情報共有": "情報共有",
}


@dataclass(frozen=True)
class InboxItem:
    node_id: UUID
    display_id: str
    title: str
    created: bool


def inbox_display_id(node_id: UUID) -> str:
    return f"IBX-{str(node_id).split('-', 1)[0].upper()}"


def _system_key(prefix: str, project_id: UUID, value: str = "") -> str:
    suffix = (
        f":{hashlib.sha256(value.encode('utf-8')).hexdigest()}" if value else ""
    )
    return f"{prefix}:{project_id}{suffix}"


def _now() -> datetime:
    return datetime.utcnow()


def _classification_label(value: str) -> str:
    return _CLASSIFICATION_LABELS.get(str(value or "").strip(), "情報共有")


def _source_type(*, has_text: bool, has_mail: bool, has_files: bool = False) -> str:
    if sum((has_text, has_mail, has_files)) > 1:
        return "複合"
    if has_mail:
        return "メール"
    return "ファイル" if has_files else "チャット"


def _title_chunks(text: str, *, limit: int = 450) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    if len(normalized) > INBOX_UPDATE_TEXT_LIMIT:
        raise ValueError(
            f"Inbox項目の更新内容は{INBOX_UPDATE_TEXT_LIMIT:,}文字以内にしてください。"
        )
    chunks: list[str] = []
    for paragraph in (part.strip() for part in normalized.split("\n") if part.strip()):
        cursor = paragraph
        while cursor:
            chunks.append(cursor[:limit])
            cursor = cursor[limit:]
    return chunks


def _document_node_count(
    document: InboxDocument,
    *,
    resolved_source_keys: set[str],
) -> int:
    def count_block(block: InboxDocumentBlock) -> int:
        return (
            len(_title_chunks(block.text))
            + sum(key in resolved_source_keys for key in block.source_keys)
            + sum(count_block(child) for child in block.children)
        )

    return (
        1
        + len(document.sections)
        + sum(count_block(block) for block in document.overview)
        + sum(
            count_block(block)
            for section in document.sections
            for block in section.blocks
        )
    )


class WorkIntakeDocsService:
    """Keep Inbox identity, generated document, and Task binding aligned."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.docs = DocsGraphService(session)

    async def document_revision(
        self,
        item: KnowledgeNode,
        *,
        lock_generated: bool = False,
    ) -> str:
        statement = (
            select(KnowledgeNode)
            .where(
                KnowledgeNode.docs_library_id == item.docs_library_id,
                KnowledgeNode.project_id == item.project_id,
                KnowledgeNode.archived_at.is_(None),
                KnowledgeNode.system_key.like(f"{item.system_key}:document:%"),
            )
            .order_by(KnowledgeNode.system_key, KnowledgeNode.id)
            .execution_options(populate_existing=True)
        )
        if lock_generated:
            statement = statement.with_for_update()
        generated = list((await self.session.execute(statement)).scalars().all())
        field_statement = (
            select(KnowledgeFieldValue)
            .where(KnowledgeFieldValue.node_id == item.id)
            .order_by(KnowledgeFieldValue.field_id)
            .execution_options(populate_existing=True)
        )
        if lock_generated:
            field_statement = field_statement.with_for_update()
        field_values = list(
            (await self.session.execute(field_statement)).scalars().all()
        )
        payload = [
            {
                "id": str(item.id),
                "title": item.title,
                "updated_at": item.updated_at.isoformat() if item.updated_at else "",
            },
            *[
                {
                    "id": str(node.id),
                    "parent_id": str(node.parent_id) if node.parent_id else "",
                    "system_key": node.system_key,
                    "title": node.title,
                    "updated_at": (
                        node.updated_at.isoformat() if node.updated_at else ""
                    ),
                }
                for node in generated
            ],
            *[
                {
                    "field_id": str(value.field_id),
                    "value_json": value.value_json,
                    "value_text": value.value_text,
                    "value_number": value.value_number,
                    "value_datetime": (
                        value.value_datetime.isoformat()
                        if value.value_datetime
                        else ""
                    ),
                    "target_node_id": (
                        str(value.target_node_id) if value.target_node_id else ""
                    ),
                    "updated_at": (
                        value.updated_at.isoformat() if value.updated_at else ""
                    ),
                }
                for value in field_values
            ],
        ]
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    async def _active_subtree_count(
        self,
        item: KnowledgeNode,
        *,
        stop_after: int,
    ) -> int:
        total = 1
        frontier = [item.id]
        while frontier and total <= stop_after:
            result = await self.session.execute(
                select(KnowledgeNode.id).where(
                    KnowledgeNode.docs_library_id == item.docs_library_id,
                    KnowledgeNode.project_id == item.project_id,
                    KnowledgeNode.parent_id.in_(frontier),
                    KnowledgeNode.archived_at.is_(None),
                )
            )
            frontier = list(result.scalars().all())
            total += len(frontier)
        return total
    async def search_items(
        self,
        *,
        user_id: UUID,
        query: str,
        limit: int = 5,
    ) -> list[RankedInboxCandidate]:
        """Search accessible real-project Inbox items without a turn project scope."""

        user = await self.session.get(User, user_id)
        if user is None:
            raise ValueError("Inboxを検索するユーザーが見つかりません。")

        # Inbox items are canonical Project Docs records.  Never scope this
        # search to the actor's personal library: a project member must see
        # the same Inbox node as every other member, while a revoked member
        # must receive no rows.  ``accessible_project_ids`` uses the shared
        # ProjectRepository ACL (owner, effective member permissions and
        # global admin) rather than a raw ProjectMember join.
        readable_project_ids: set[UUID] | None = None
        if str(getattr(user, "role", "")).lower() != "admin":
            readable_project_ids = set(
                await accessible_project_ids(self.session, user_id)
            )
            if not readable_project_ids:
                return []

        management_node = aliased(KnowledgeNode)
        stmt = (
            select(KnowledgeNode, Project)
            .join(Project, Project.id == KnowledgeNode.project_id)
            .join(
                DocsLibrary,
                DocsLibrary.id == KnowledgeNode.docs_library_id,
            )
            .join(
                management_node,
                management_node.id == KnowledgeNode.parent_id,
            )
            .where(
                KnowledgeNode.system_key.startswith(
                    f"{INBOX_ITEM_SYSTEM_PREFIX}:"
                ),
                management_node.docs_library_id == KnowledgeNode.docs_library_id,
                management_node.system_key.startswith(
                    f"{INBOX_MANAGEMENT_SYSTEM_PREFIX}:"
                ),
                KnowledgeNode.archived_at.is_(None),
                management_node.archived_at.is_(None),
                Project.deleted_at.is_(None),
                KnowledgeNode.project_id.in_(readable_project_ids)
                if readable_project_ids is not None
                else True,
            )
        )
        # ``readable_project_ids`` is deliberately applied to the node identity
        # in SQL so a private/revoked project's Inbox never enters the result.

        rows = (await self.session.execute(stmt)).all()
        rows = [
            (node, project)
            for node, project in rows
            if not is_default_inbox_project(project)
        ]
        if not rows:
            return []

        item_ids = [node.id for node, _project in rows]
        field_rows = await self.session.execute(
            select(
                KnowledgeFieldValue.node_id,
                KnowledgeField.system_key,
                KnowledgeFieldValue.value_text,
            )
            .join(
                KnowledgeField,
                KnowledgeField.id == KnowledgeFieldValue.field_id,
            )
            .join(
                KnowledgeNode,
                KnowledgeNode.id == KnowledgeFieldValue.node_id,
            )
            .where(
                KnowledgeFieldValue.node_id.in_(item_ids),
                KnowledgeField.docs_library_id == KnowledgeNode.docs_library_id,
            )
        )
        fields_by_item: dict[UUID, list[str]] = {}
        for node_id, system_key, value_text in field_rows.all():
            if system_key not in {
                "inbox_instruction",
                "inbox_summary",
                "inbox_item_id",
            }:
                continue
            if str(value_text or "").strip():
                fields_by_item.setdefault(node_id, []).append(str(value_text))

        item_id_by_system_key = {
            str(node.system_key): node.id
            for node, _project in rows
            if str(node.system_key or "").strip()
        }
        descendant_text_by_item: dict[UUID, list[str]] = {}
        descendant_rows = await self.session.execute(
            select(KnowledgeNode.system_key, KnowledgeNode.title)
            .join(
                DocsLibrary,
                DocsLibrary.id == KnowledgeNode.docs_library_id,
            )
            .where(
                KnowledgeNode.project_id.in_(
                    {project.id for _node, project in rows}
                ),
                KnowledgeNode.system_key.startswith(
                    f"{INBOX_ITEM_SYSTEM_PREFIX}:"
                ),
                KnowledgeNode.archived_at.is_(None),
            )
        )
        for descendant_key, descendant_title in descendant_rows.all():
            key = str(descendant_key or "")
            root_key = ":".join(key.split(":")[:3])
            item_id = item_id_by_system_key.get(root_key)
            if item_id is None or key == root_key:
                continue
            title = str(descendant_title or "").strip()
            if title:
                descendant_text_by_item.setdefault(item_id, []).append(title)

        candidates = [
            InboxSearchCandidate(
                node_id=node.id,
                project_id=project.id,
                project_name=project.name,
                title=node.title,
                searchable_text="\n".join(
                    [
                        str(node.description or ""),
                        *fields_by_item.get(node.id, []),
                        *descendant_text_by_item.get(node.id, []),
                    ]
                ),
                updated_at=node.updated_at,
            )
            for node, project in rows
        ]
        return rank_inbox_candidates(query, candidates)[: max(1, min(limit, 20))]

    async def _find_system_node(
        self,
        *,
        docs_library_id: UUID,
        project_id: UUID,
        system_key: str,
    ) -> KnowledgeNode | None:
        result = await self.session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.docs_library_id == docs_library_id,
                KnowledgeNode.project_id == project_id,
                KnowledgeNode.system_key == system_key,
            )
        )
        return result.scalar_one_or_none()

    async def _create_unique_node(
        self, **kwargs: Any
    ) -> tuple[KnowledgeNode, bool]:
        existing = await self._find_system_node(
            docs_library_id=kwargs["docs_library_id"],
            project_id=kwargs["project_id"],
            system_key=kwargs["system_key"],
        )
        if existing is not None:
            parent = kwargs.get("parent")
            changed = False
            if parent is not None:
                expected_root_id = parent.root_page_id or parent.id
                if (
                    existing.parent_id != parent.id
                    or existing.root_page_id != expected_root_id
                ):
                    existing.parent_id = parent.id
                    existing.root_page_id = expected_root_id
                    changed = True
            if existing.archived_at is not None:
                existing.archived_at = None
                changed = True
            requested_title = str(kwargs.get("title") or "").strip()
            if requested_title and existing.title != requested_title:
                existing.title = requested_title[:500]
                existing.body_text = existing.title
                changed = True
            if changed:
                existing.updated_by = kwargs["user_id"]
                await self.docs.record_node_change(
                    existing,
                    kwargs["user_id"],
                    "Inbox構造を修復",
                )
                await self.session.flush()
            return existing, False
        try:
            async with self.session.begin_nested():
                node = await self.docs.create_node(**kwargs)
            return node, True
        except IntegrityError:
            existing = await self._find_system_node(
                docs_library_id=kwargs["docs_library_id"],
                project_id=kwargs["project_id"],
                system_key=kwargs["system_key"],
            )
            if existing is None:
                raise
            return existing, False

    async def _resolve_project(
        self, *, project_id: UUID, user_id: UUID
    ) -> Project:
        project = await self.session.scalar(
            select(Project)
            .where(Project.id == project_id, Project.deleted_at.is_(None))
            .with_for_update()
        )
        if project is None:
            raise ValueError("Inboxの保存先プロジェクトが見つかりません。")
        if is_default_inbox_project(project):
            raise ValueError(
                "/inboxは実プロジェクトを選択して実行してください。"
                "既定のInboxプロジェクトは保存先にできません。"
            )
        return project

    async def ensure_management_node(
        self,
        *,
        project_id: UUID,
        user_id: UUID,
    ) -> KnowledgeNode:
        project = await self._resolve_project(project_id=project_id, user_id=user_id)
        # Project Inbox management belongs to the canonical ownerless Project
        # Docs library.  Never pair it with the actor's personal library:
        # the project-information parent and all Inbox descendants must share
        # one library/ACL boundary.
        library = await self.docs.ensure_project_information_library(project.id, user_id)
        project_information = await ensure_project_information_doc(
            self.session,
            project=project,
            user_id=user_id,
        )
        if project_information.docs_library_id != library.id:
            raise ValueError("案件情報とInboxのworkspaceが一致しません")
        management, _ = await self._create_unique_node(
            # Bind the child to the actual parent row returned by the
            # canonical project-information helper.  The graph service also
            # rejects cross-library parents, but using this ID here keeps
            # the invariant explicit if a legacy/migrating library lookup
            # ever disagrees.
            docs_library_id=project_information.docs_library_id,
            user_id=user_id,
            title=INBOX_MANAGEMENT_TITLE,
            parent=project_information,
            project_id=project.id,
            system_key=_system_key(INBOX_MANAGEMENT_SYSTEM_PREFIX, project.id),
            body_json={"format": "work_intake_collection", "project_id": str(project.id)},
        )
        return management

    async def create_item(
        self,
        *,
        project_id: UUID,
        user_id: UUID,
        source_key: str,
        title: str,
        classification: str,
        instruction: str,
        summary: str,
        status: str,
        source_node_ids: Iterable[UUID] = (),
        source_refs: list[dict[str, Any]] | None = None,
        has_mail: bool = False,
        has_files: bool = False,
    ) -> InboxItem:
        management = await self.ensure_management_node(
            project_id=project_id,
            user_id=user_id,
        )
        clean_source_key = str(source_key or "").strip()
        if not clean_source_key:
            raise ValueError("Inbox項目の受付識別子がありません。")
        item, created = await self._create_unique_node(
            docs_library_id=management.docs_library_id,
            user_id=user_id,
            title=str(title or "受付内容の確認").strip()[:500],
            parent=management,
            project_id=project_id,
            system_key=_system_key(
                INBOX_ITEM_SYSTEM_PREFIX,
                project_id,
                clean_source_key,
            ),
            body_json={
                "format": "work_intake_item",
                "source_key_sha256": hashlib.sha256(
                    clean_source_key.encode("utf-8")
                ).hexdigest(),
            },
            source_refs=source_refs or [],
        )
        tag = await self.docs.resolve_supertag(
            docs_library_id=item.docs_library_id,
            tag=INBOX_SUPERTAG_SYSTEM_KEY,
            create=False,
        )
        await self.docs.add_tag(node=item, tag=tag, user_id=user_id)
        timestamp = _now()
        field_values = {
            "inbox_item_id": inbox_display_id(item.id),
            "inbox_classification": _classification_label(classification),
            "inbox_status": status,
            "inbox_source_type": _source_type(
                has_text=bool(str(instruction or "").strip()),
                has_mail=has_mail,
                has_files=has_files,
            ),
            "inbox_received_at": timestamp.isoformat(),
            "inbox_last_updated_at": timestamp.isoformat(),
            "inbox_instruction": str(instruction or "").strip(),
            "inbox_summary": str(summary or "").strip(),
        }
        await self.docs.set_fields(
            node=item,
            values={
                key: value
                for key, value in field_values.items()
                if value not in ("", None)
            },
            user_id=user_id,
        )
        await self.docs.record_node_change(
            item,
            user_id,
            "Inbox項目を受付",
            source_refs or [],
        )
        await self.session.flush()
        return InboxItem(
            node_id=item.id,
            display_id=inbox_display_id(item.id),
            title=item.title,
            created=created,
        )

    async def attach_source_files(
        self,
        *,
        item_id: UUID,
        user_id: UUID,
        files: Iterable[dict[str, str]],
    ) -> list[UUID]:
        """Inbox項目直下へ添付原本を指すリンク子ノードを作る。

        `replace_document` の再構成対象(`:document:` 配下)には入れないため、
        文書を作り直してもリンクは保持される。
        """
        item = await self.session.get(KnowledgeNode, item_id)
        if item is None:
            raise ValueError("Inbox項目が見つかりません。")
        created: list[UUID] = []
        for attachment in files:
            path = str(attachment.get("path") or "").strip()
            if not path:
                continue
            name = str(attachment.get("name") or "").strip() or path.rsplit("/", 1)[-1]
            label = name.replace("]", "").replace("|", "")[:200]
            item_library_id = getattr(
                item,
                "docs_library_id",
                getattr(item, "workspace_id", None),
            )
            if item_library_id is None:
                raise ValueError("Inbox項目のDocs Libraryが見つかりません。")
            node, _ = await self._create_unique_node(
                docs_library_id=item_library_id,
                user_id=user_id,
                title=f"[[file:{path}|{label}]]"[:500],
                parent=item,
                project_id=item.project_id,
                system_key=(
                    f"{item.system_key}:attachment:"
                    f"{hashlib.sha256(path.encode('utf-8')).hexdigest()[:32]}"
                ),
                body_json={"format": "work_intake_attachment_link"},
                source_refs=[{"type": "workspace_file", "path": path}],
            )
            created.append(node.id)
        await self.session.flush()
        return created

    async def _create_document_block(
        self,
        *,
        item: KnowledgeNode,
        parent: KnowledgeNode,
        block: InboxDocumentBlock,
        source_nodes: dict[str, KnowledgeNode],
        user_id: UUID,
        generation_key: str,
        path: str,
    ) -> None:
        chunks = _title_chunks(block.text)
        if not chunks:
            return
        last: KnowledgeNode | None = None
        for index, chunk in enumerate(chunks):
            node = await self.docs.create_node(
                docs_library_id=item.docs_library_id,
                user_id=user_id,
                title=chunk,
                parent=parent,
                project_id=item.project_id,
                system_key=f"{item.system_key}:document:{generation_key}:{path}:text:{index}",
                body_json={"format": "work_intake_generated_block"},
            )
            last = node
        local_parent = last or parent
        for source_index, source_key in enumerate(block.source_keys):
            source = source_nodes.get(source_key)
            if source is None:
                continue
            label = source.title.replace("]", "")[:320]
            await self.docs.create_node(
                docs_library_id=item.docs_library_id,
                user_id=user_id,
                title=f"[[node:{source.id}|根拠: {label}]]",
                parent=local_parent,
                project_id=item.project_id,
                system_key=(
                    f"{item.system_key}:document:{generation_key}:"
                    f"{path}:source:{source_index}"
                ),
                body_json={"format": "work_intake_inline_source"},
                source_refs=[{"type": "docs_node", "id": str(source.id)}],
            )
        for child_index, child in enumerate(block.children):
            await self._create_document_block(
                item=item,
                parent=local_parent,
                block=child,
                source_nodes=source_nodes,
                user_id=user_id,
                generation_key=generation_key,
                path=f"{path}:child:{child_index}",
            )

    async def replace_document(
        self,
        *,
        item_id: UUID,
        project_id: UUID,
        user_id: UUID,
        document: InboxDocument,
        source_nodes: dict[str, UUID],
        status: str = "",
        source_refs: list[dict[str, Any]] | None = None,
        expected_revision: str = "",
    ) -> InboxItem:
        # The target node is authoritative.  Project Docs now live in a
        # canonical ownerless library, while older callers may still pass a
        # personal library derived from ``user_id``.  Resolving the library
        # from the target avoids silently looking in the wrong tree.
        target = await self.session.get(KnowledgeNode, item_id)
        if target is None or not await can_write_node(
            self.session, target, user_id
        ):
            raise ValueError("指定したDocsノードはInbox項目ではありません。")
        docs_library_id = target.docs_library_id
        result = await self.session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.id == item_id,
                KnowledgeNode.docs_library_id == docs_library_id,
                KnowledgeNode.project_id == project_id,
                KnowledgeNode.archived_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        item = result.scalar_one_or_none()
        if item is None or not str(item.system_key or "").startswith(
            f"{INBOX_ITEM_SYSTEM_PREFIX}:{project_id}:"
        ):
            raise ValueError("指定したDocsノードはInbox項目ではありません。")
        current_revision = await self.document_revision(
            item,
            lock_generated=True,
        )
        if expected_revision and expected_revision != current_revision:
            raise ValueError(
                "Inbox項目は読み取り後に更新されています。docs_readで再読込してから"
                "文書全体を再構成してください。"
            )
        if (
            await self._active_subtree_count(
                item,
                stop_after=500,
            )
            > 500
        ):
            raise ValueError(
                "Inbox項目が500ノードを超えているため、安全に全文更新できません。"
                "内容を整理してから再実行してください。"
            )
        if status and status not in INBOX_STATUS_VALUES:
            raise ValueError("Inbox項目の対応状態が不正です。")

        requested_ids = set(source_nodes.values())
        source_result = await self.session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.id.in_(requested_ids),
                KnowledgeNode.docs_library_id == docs_library_id,
                KnowledgeNode.project_id == project_id,
                KnowledgeNode.archived_at.is_(None),
            )
        )
        valid_by_id = {node.id: node for node in source_result.scalars().all()}
        if requested_ids != set(valid_by_id):
            raise ValueError("Inbox文書の根拠に対象プロジェクト外のノードがあります。")
        resolved_sources = {
            key: valid_by_id[node_id]
            for key, node_id in source_nodes.items()
            if node_id in valid_by_id
        }
        generated_node_count = _document_node_count(
            document,
            resolved_source_keys=set(resolved_sources),
        )
        if generated_node_count > INBOX_GENERATED_NODE_LIMIT:
            raise ValueError(
                "Inbox文書が大きすぎます。概要を保ったまま内容を圧縮してください。"
            )

        direct_children = await self.session.execute(
            select(KnowledgeNode).where(
                KnowledgeNode.parent_id == item.id,
                KnowledgeNode.archived_at.is_(None),
            )
        )
        for child in direct_children.scalars().all():
            child_key = str(child.system_key or "")
            child_format = str((child.body_json or {}).get("format") or "")
            if (
                child_key.startswith(f"{item.system_key}:document:")
                or child_key in {
                    f"{item.system_key}:sources",
                    f"{item.system_key}:updates",
                }
                or child_format in {
                    "work_intake_generated_document",
                    "work_intake_sources",
                    "work_intake_updates",
                }
            ):
                await self.docs.archive_subtree(root=child, user_id=user_id)

        await self.docs.update_node(
            node=item,
            user_id=user_id,
            title=document.title,
            source_refs=source_refs or [],
            change_summary="Inbox文書を再構成",
        )
        generation_key = str(uuid4())
        sections = [
            ("概要", document.overview),
            *((section.title, section.blocks) for section in document.sections),
        ]
        for section_index, (section_title, blocks) in enumerate(sections):
            section_node = await self.docs.create_node(
                docs_library_id=item.docs_library_id,
                user_id=user_id,
                title=section_title,
                parent=item,
                project_id=project_id,
                system_key=(
                    f"{item.system_key}:document:{generation_key}:"
                    f"section:{section_index}"
                ),
                body_json={"format": "work_intake_generated_document"},
            )
            for block_index, block in enumerate(blocks):
                await self._create_document_block(
                    item=item,
                    parent=section_node,
                    block=block,
                    source_nodes=resolved_sources,
                    user_id=user_id,
                    generation_key=generation_key,
                    path=f"section:{section_index}:block:{block_index}",
                )
        values: dict[str, Any] = {
            "inbox_summary": document.summary_text(),
            "inbox_last_updated_at": _now().isoformat(),
        }
        if status:
            values["inbox_status"] = status
        await self.docs.set_fields(node=item, values=values, user_id=user_id)
        await self.session.flush()
        return InboxItem(
            node_id=item.id,
            display_id=inbox_display_id(item.id),
            title=item.title,
            created=False,
        )

    async def bind_task(
        self,
        *,
        item_id: UUID,
        task_id: UUID,
        user_id: UUID,
    ) -> None:
        item = await self.session.get(KnowledgeNode, item_id)
        task = await self.session.get(Task, task_id)
        if item is None or task is None or task.deleted_at is not None:
            raise ValueError("Inbox項目またはタスクが見つかりません。")
        if item.project_id != task.project_id:
            raise ValueError("Inbox項目とタスクのプロジェクトが一致しません。")
        task.knowledge_node_id = item.id
        task_tag = await self.docs.resolve_supertag(
            docs_library_id=item.docs_library_id,
            tag="task",
            create=False,
        )
        await self.docs.add_tag(node=item, tag=task_tag, user_id=user_id)
        await self.docs.record_node_change(item, user_id, "タスクへ紐付け")
        await self.session.flush()
