"""Shared service layer for AoiTalk Docs graph operations."""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import String, and_, cast, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models import (
    KnowledgeEdge,
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    KnowledgeNodePlacement,
    KnowledgeNodeSupertag,
    KnowledgeRevision,
    KnowledgeSearchIndex,
    KnowledgeSupertag,
    Project,
    Task,
)
from ..task_time import DEFAULT_TASK_TIMEZONE
from .docs_workspace import ensure_docs_workspace
from .task_management_service import TaskManagementService


SYSTEM_TASK_TAG = "task"
TASK_FIELD_TO_TASK_UPDATE = {
    "task_status": "status",
    "task_due": "end_at",
    "task_start": "start_at",
    "task_priority": "priority",
    "task_project": "project_id",
}

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_NODE_TOKEN_RE = re.compile(
    r"\[\[node:([0-9a-fA-F-]{36})(?:\|[^\]]*)?\]\]|@docs:([0-9a-fA-F-]{36})"
)
_TAG_TOKEN_RE = re.compile(r"(?:^|\s)#([^\s#:\[]+)")
_FIELD_TOKEN_RE = re.compile(r"([^|#\n]{1,80})::\s*([^|#\n]+)")


@dataclass(frozen=True)
class ParsedOutlineLine:
    depth: int
    title: str
    tags: tuple[str, ...]
    fields: dict[str, str]


def _now() -> datetime:
    return datetime.utcnow()


def _title_mirror(title: Any) -> str:
    """title 由来の検索ミラー本文を返す（不変条件: 改行禁止・500字以内）。

    Web `docsNodeTitleMirror`（docs-node-writer.ts）と同一挙動。body_text は本文正本
    ではなく title のミラーであり、Web↔モバイル往復で検索インデックス・暗号化を
    一致させるためここで一元生成する。
    """
    mirror = str(title or "").strip()
    if "\n" in mirror or "\r" in mirror:
        raise ValueError("Docs node body_text mirror must not contain newlines")
    return mirror[:500]


def _short_id(value: uuid.UUID | str | None) -> str:
    return str(value or "")[:8]


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _parse_outline_text(outline_text: str) -> list[ParsedOutlineLine]:
    lines: list[ParsedOutlineLine] = []
    for raw_line in str(outline_text or "").replace("\r\n", "\n").splitlines():
        if not raw_line.strip():
            continue
        expanded = raw_line.replace("    ", "\t")
        depth = 0
        while depth < len(expanded) and expanded[depth] == "\t":
            depth += 1
        content = expanded[depth:].strip()
        if not content:
            continue
        content = re.sub(r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|\[[ xX]\]\s+)", "", content).strip()
        if not content:
            continue

        fields = {
            match.group(1).strip(): match.group(2).strip()
            for match in _FIELD_TOKEN_RE.finditer(content)
            if match.group(1).strip()
        }
        content_without_fields = _FIELD_TOKEN_RE.sub("", content)
        tags = tuple(
            dict.fromkeys(
                tag.strip()
                for tag in _TAG_TOKEN_RE.findall(content_without_fields)
                if tag.strip()
            )
        )
        title = _TAG_TOKEN_RE.sub("", content_without_fields).strip(" -|\t")
        if not title:
            title = "Untitled"
        chunks: list[str] = []
        remaining = title
        while len(remaining) > 500:
            boundary = max(
                remaining.rfind("。", 0, 500),
                remaining.rfind("！", 0, 500),
                remaining.rfind("？", 0, 500),
                remaining.rfind(" ", 0, 500),
            )
            cut = boundary + 1 if boundary >= 200 else 500
            chunks.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        if remaining:
            chunks.append(remaining)
        for index, chunk in enumerate(chunks):
            lines.append(
                ParsedOutlineLine(
                    depth=depth,
                    title=chunk,
                    tags=tags if index == 0 else (),
                    fields=fields if index == 0 else {},
                )
            )
    return lines


def _notify_docs_node_changed(workspace_id: uuid.UUID, node_id: uuid.UUID) -> None:
    """Best-effort hook to mark a Docs node for RAG re-indexing.

    Kept fully guarded: when the Docs RAG index is disabled (the default) or the
    optional dependency stack is missing, this must be a cheap no-op and must
    never raise, because it runs inside every Docs mutation transaction.
    """
    try:
        from ..rag.docs_index import enqueue_docs_reindex

        enqueue_docs_reindex(workspace_id, node_id)
    except Exception:
        return


class DocsGraphService:
    """Operate on Docs nodes while preserving revisions and derived indexes."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def ensure_workspace(self, user_id: uuid.UUID | None):
        return await ensure_docs_workspace(self.session, owner_user_id=user_id)

    async def resolve_node(
        self,
        *,
        workspace_id: uuid.UUID,
        ref: str,
        project_id: uuid.UUID | None = None,
        allow_archived: bool = False,
    ) -> KnowledgeNode:
        text = str(ref or "").strip()
        if not text:
            raise ValueError("node reference is required")

        if text.casefold() == "today":
            node, _, _ = await self.ensure_daily_page(
                workspace_id=workspace_id,
                user_id=None,
                day=date.today(),
            )
            return node

        # UUID として解釈できる参照は正規化して直接解決する。
        # （クライアント生成 ID がハイフン位置の異なる 32hex で届いても、
        #   uuid.UUID() の寛容パースにより create 時と同じ正規形へ揃う）
        parsed_uuid = _coerce_uuid(text)
        if parsed_uuid is not None:
            node = await self.session.get(KnowledgeNode, parsed_uuid)
            if (
                node
                and node.workspace_id == workspace_id
                and (project_id is None or node.project_id == project_id)
                and (allow_archived or node.archived_at is None)
            ):
                return node
            if _UUID_RE.match(text):
                raise ValueError(f"node not found: {text}")
            # 非正規形はタイトル一致などのフォールバックに委ねる

        if re.fullmatch(r"[0-9a-fA-F]{8,12}", text):
            conditions = [
                KnowledgeNode.workspace_id == workspace_id,
                cast(KnowledgeNode.id, String).ilike(f"{text}%"),
            ]
            if project_id is not None:
                conditions.append(KnowledgeNode.project_id == project_id)
            if not allow_archived:
                conditions.append(KnowledgeNode.archived_at.is_(None))
            result = await self.session.execute(
                select(KnowledgeNode).where(*conditions)
            )
            matches = list(result.scalars().all())
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise ValueError(f"node prefix is ambiguous: {text}")

        conditions: list[Any] = [
            KnowledgeNode.workspace_id == workspace_id,
            KnowledgeNode.title == text,
        ]
        if not allow_archived:
            conditions.append(KnowledgeNode.archived_at.is_(None))
        if project_id is not None:
            conditions.append(KnowledgeNode.project_id == project_id)
        result = await self.session.execute(
            select(KnowledgeNode).where(*conditions).order_by(KnowledgeNode.updated_at.desc()).limit(2)
        )
        matches = list(result.scalars().all())
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"node reference is ambiguous: {text}")
        raise ValueError(f"node not found: {text}")

    async def resolve_supertag(
        self,
        *,
        workspace_id: uuid.UUID,
        tag: str,
        create: bool = True,
    ) -> KnowledgeSupertag:
        text = str(tag or "").strip().lstrip("#")
        if not text:
            raise ValueError("tag is required")
        parsed_uuid = _coerce_uuid(text)
        if parsed_uuid is not None:
            row = await self.session.get(KnowledgeSupertag, parsed_uuid)
            if row and row.workspace_id == workspace_id:
                return row
            raise ValueError(f"supertag not found: {tag}")

        result = await self.session.execute(
            select(KnowledgeSupertag)
            .where(
                KnowledgeSupertag.workspace_id == workspace_id,
                or_(
                    KnowledgeSupertag.system_key == text.casefold(),
                    func.lower(KnowledgeSupertag.name) == text.casefold(),
                ),
            )
            .order_by(KnowledgeSupertag.created_at)
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return row
        if not create:
            raise ValueError(f"supertag not found: {tag}")
        row = KnowledgeSupertag(
            workspace_id=workspace_id,
            name=text[:120],
            base_type="note",
            color="#64748b",
            template_json={},
            pinned_field_ids=[],
            config_json={},
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def resolve_project(self, project_ref: str = "") -> Project | None:
        text = str(project_ref or "").strip()
        if not text:
            return None
        parsed_uuid = _coerce_uuid(text)
        conditions = [Project.deleted_at.is_(None)]
        if parsed_uuid is not None:
            conditions.append(Project.id == parsed_uuid)
        else:
            conditions.append(
                or_(
                    func.lower(Project.slug) == text.casefold(),
                    func.lower(Project.name) == text.casefold(),
                    Project.name.ilike(f"%{text}%"),
                )
            )
        result = await self.session.execute(select(Project).where(*conditions).limit(2))
        matches = list(result.scalars().all())
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"project reference is ambiguous: {project_ref}")
        return None

    async def upsert_search_index(self, node: KnowledgeNode) -> None:
        row = await self.session.get(KnowledgeSearchIndex, node.id)
        if row is None:
            row = KnowledgeSearchIndex(node_id=node.id)
            self.session.add(row)
        row.workspace_id = node.workspace_id
        row.project_id = node.project_id
        row.title_text = node.title or ""
        row.body_text_plain = node.body_text or ""
        row.updated_at = _now()

    async def sync_reference_edges(self, node: KnowledgeNode, user_id: uuid.UUID | None) -> None:
        await self.session.execute(
            delete(KnowledgeEdge).where(
                KnowledgeEdge.source_node_id == node.id,
                KnowledgeEdge.relation_type.in_(["inline_ref", "references"]),
            )
        )
        text = "\n".join([node.title or "", node.body_text or ""])
        target_ids: list[uuid.UUID] = []
        for match in _NODE_TOKEN_RE.finditer(text):
            value = match.group(1) or match.group(2)
            parsed = _coerce_uuid(value)
            if parsed is not None and parsed != node.id and parsed not in target_ids:
                target_ids.append(parsed)
        if not target_ids:
            return
        existing_result = await self.session.execute(
            select(KnowledgeNode.id).where(
                KnowledgeNode.workspace_id == node.workspace_id,
                KnowledgeNode.id.in_(target_ids),
                KnowledgeNode.archived_at.is_(None),
            )
        )
        existing_ids = set(existing_result.scalars().all())
        for target_id in target_ids:
            if target_id not in existing_ids:
                continue
            self.session.add(
                KnowledgeEdge(
                    source_node_id=node.id,
                    target_node_id=target_id,
                    relation_type="inline_ref",
                    confidence=1,
                    created_by=user_id,
                )
            )

    async def record_node_change(
        self,
        node: KnowledgeNode,
        user_id: uuid.UUID | None,
        change_summary: str,
        source_refs: list[dict[str, Any]] | None = None,
    ) -> None:
        await self.upsert_search_index(node)
        await self.sync_reference_edges(node, user_id)
        self.session.add(
            KnowledgeRevision(
                node_id=node.id,
                title=node.title or "",
                body_json=node.body_json or {},
                body_text=node.body_text or "",
                change_summary=change_summary,
                source_refs_json=source_refs or [],
                created_by=user_id,
            )
        )
        _notify_docs_node_changed(node.workspace_id, node.id)

    async def _next_sort_order(self, parent_id: uuid.UUID | None, workspace_id: uuid.UUID) -> float:
        result = await self.session.execute(
            select(func.max(KnowledgeNode.sort_order)).where(
                KnowledgeNode.workspace_id == workspace_id,
                KnowledgeNode.parent_id == parent_id,
            )
        )
        current = result.scalar_one_or_none()
        return float(current or 0) + 1

    async def create_node(
        self,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID | None,
        title: str,
        parent: KnowledgeNode | None = None,
        project_id: uuid.UUID | None = None,
        body_text: str = "",
        body_json: dict[str, Any] | None = None,
        node_type: str = "node",
        sort_order: float | None = None,
        node_id: uuid.UUID | None = None,
        system_key: str | None = None,
        day_date: date | None = None,
        source_refs: list[dict[str, Any]] | None = None,
    ) -> KnowledgeNode:
        if project_id is not None and parent is None:
            raise ValueError(
                "Project-scoped Docs nodes require a parent under 案件情報"
            )
        root_page_id = None
        if parent is not None:
            root_page_id = parent.root_page_id or parent.id
            if project_id is None:
                project_id = parent.project_id
        clean_title = (title or "Untitled").strip()[:500]
        # 不変条件(1.6a): 本文は子node階層が正本。body_text は常にtitle mirror。
        # Python organizer経路だけ任意本文を許す例外を残すと、Web/モバイルとの
        # 往復で巨大title・二重正本が再発するため、非mirror値は明示的に拒否する。
        body_text_value = _title_mirror(clean_title)
        if str(body_text or "").strip() not in {"", body_text_value}:
            raise ValueError("Docs body content must be represented by child nodes")
        node = KnowledgeNode(
            id=node_id if node_id is not None else uuid.uuid4(),
            workspace_id=workspace_id,
            parent_id=parent.id if parent else None,
            root_page_id=root_page_id,
            project_id=project_id,
            system_key=system_key,
            title=clean_title,
            body_text=body_text_value,
            body_json=body_json or {},
            node_type=node_type,
            day_date=day_date,
            sort_order=sort_order
            if sort_order is not None
            else await self._next_sort_order(parent.id if parent else None, workspace_id),
            created_by=user_id,
            updated_by=user_id,
        )
        self.session.add(node)
        await self.session.flush()
        await self.record_node_change(node, user_id, "nodeを作成", source_refs)
        await self.session.flush()
        return node

    async def update_node(
        self,
        *,
        node: KnowledgeNode,
        user_id: uuid.UUID | None,
        title: str | None = None,
        description: str | None = None,
        body_json: dict[str, Any] | None = None,
        body_text: str | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        change_summary: str = "nodeを更新",
    ) -> KnowledgeNode:
        if title is not None and title.strip():
            node.title = title.strip()[:500]
            # 不変条件(1.6a): title 変更のたび body_text ミラーを再計算する。
            node.body_text = _title_mirror(node.title)
            await self._sync_bound_task_title(node=node, user_id=user_id)
        if description is not None:
            node.description = str(description)[:200000]
        if body_json is not None:
            node.body_json = body_json
        if body_text is not None:
            requested = str(body_text).strip()
            mirror = _title_mirror(node.title)
            if requested not in {"", mirror}:
                raise ValueError("Docs body content must be represented by child nodes")
            node.body_text = mirror
        node.updated_by = user_id
        node.updated_at = _now()
        await self.record_node_change(node, user_id, change_summary, source_refs)
        await self.session.flush()
        return node

    async def add_tag(
        self,
        *,
        node: KnowledgeNode,
        tag: KnowledgeSupertag,
        user_id: uuid.UUID | None,
    ) -> bool:
        link = await self.session.get(
            KnowledgeNodeSupertag,
            {"node_id": node.id, "supertag_id": tag.id},
        )
        if link is not None:
            return False
        self.session.add(
            KnowledgeNodeSupertag(node_id=node.id, supertag_id=tag.id, created_by=user_id)
        )
        await self.session.flush()
        if tag.system_key == SYSTEM_TASK_TAG:
            await self._ensure_bound_task(node=node, user_id=user_id)
        return True

    async def remove_tag(
        self,
        *,
        node: KnowledgeNode,
        tag: KnowledgeSupertag,
        user_id: uuid.UUID | None,
    ) -> bool:
        link = await self.session.get(
            KnowledgeNodeSupertag,
            {"node_id": node.id, "supertag_id": tag.id},
        )
        if link is None:
            return False
        await self.session.delete(link)
        await self.session.flush()
        if tag.system_key == SYSTEM_TASK_TAG:
            await self._unlink_bound_task(node=node, user_id=user_id)
        return True

    async def _ensure_bound_task(self, *, node: KnowledgeNode, user_id: uuid.UUID | None) -> None:
        if user_id is None:
            return
        existing = await self.session.execute(
            select(Task.id).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            return
        await TaskManagementService().create_task(
            self.session,
            user_id=user_id,
            project_id=node.project_id,
            knowledge_node_id=node.id,
            title=node.title or "Untitled",
            description=node.description or None,
            source="docs",
            status="todo",
            priority="medium",
            task_metadata={"source": "docs", "knowledge_node_id": str(node.id)},
        )

    async def _unlink_bound_task(self, *, node: KnowledgeNode, user_id: uuid.UUID | None) -> None:
        if user_id is None:
            return
        result = await self.session.execute(
            select(Task).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return
        await TaskManagementService().update_task(
            self.session,
            user_id=user_id,
            task_id=task.id,
            updates={"knowledge_node_id": None},
        )

    async def _sync_bound_task_title(self, *, node: KnowledgeNode, user_id: uuid.UUID | None) -> None:
        if user_id is None:
            return
        result = await self.session.execute(
            select(Task).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        task = result.scalar_one_or_none()
        if task is None or task.title == node.title:
            return
        await TaskManagementService().update_task(
            self.session,
            user_id=user_id,
            task_id=task.id,
            updates={"title": node.title},
        )

    async def resolve_node_fields(self, node: KnowledgeNode) -> dict[str, KnowledgeField]:
        tag_result = await self.session.execute(
            select(KnowledgeSupertag.id)
            .join(KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(KnowledgeNodeSupertag.node_id == node.id)
        )
        tag_ids = list(tag_result.scalars().all())
        if not tag_ids:
            return {}
        field_result = await self.session.execute(
            select(KnowledgeField)
            .where(KnowledgeField.supertag_id.in_(tag_ids))
            .order_by(KnowledgeField.sort_order, KnowledgeField.created_at)
        )
        fields: dict[str, KnowledgeField] = {}
        for field in field_result.scalars().all():
            fields[field.name.casefold()] = field
            if field.system_key:
                fields[field.system_key.casefold()] = field
        return fields

    async def set_fields(
        self,
        *,
        node: KnowledgeNode,
        values: dict[str, Any],
        user_id: uuid.UUID | None,
    ) -> dict[str, str]:
        fields_by_ref = await self.resolve_node_fields(node)
        updated: dict[str, str] = {}
        task_updates: dict[str, Any] = {}
        for field_ref, raw_value in values.items():
            field = fields_by_ref.get(str(field_ref).casefold())
            if field is None:
                raise ValueError(f"field not found on node tags: {field_ref}")
            if field.system_key in TASK_FIELD_TO_TASK_UPDATE:
                task_updates[TASK_FIELD_TO_TASK_UPDATE[field.system_key]] = self._coerce_task_field_value(
                    field.system_key,
                    raw_value,
                )
                updated[field.name] = "task"
                continue
            await self._set_field_value(node=node, field=field, raw_value=raw_value, user_id=user_id)
            updated[field.name] = "docs"
        if task_updates:
            await self._update_bound_task(node=node, user_id=user_id, updates=task_updates)
        await self.session.flush()
        return updated

    def _coerce_task_field_value(self, system_key: str, raw_value: Any) -> Any:
        if raw_value in ("", None):
            return None
        if system_key in {"task_due", "task_start"}:
            return _parse_datetime(raw_value)
        if system_key == "task_project":
            return _coerce_uuid(raw_value)
        return str(raw_value)

    async def _update_bound_task(
        self,
        *,
        node: KnowledgeNode,
        user_id: uuid.UUID | None,
        updates: dict[str, Any],
    ) -> None:
        if user_id is None:
            raise ValueError("user_id is required for task field updates")
        result = await self.session.execute(
            select(Task).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise ValueError("node is not bound to a task")
        await TaskManagementService().update_task(
            self.session,
            user_id=user_id,
            task_id=task.id,
            updates=updates,
        )

    async def _set_field_value(
        self,
        *,
        node: KnowledgeNode,
        field: KnowledgeField,
        raw_value: Any,
        user_id: uuid.UUID | None,
    ) -> None:
        row = await self.session.get(
            KnowledgeFieldValue,
            {"node_id": node.id, "field_id": field.id},
        )
        if row is None:
            row = KnowledgeFieldValue(node_id=node.id, field_id=field.id)
            self.session.add(row)
        row.value_json = None
        row.value_text = None
        row.value_number = None
        row.value_datetime = None
        row.target_node_id = None
        if raw_value in ("", None):
            await self.session.delete(row)
            return
        field_type = str(field.field_type or "text")
        if field_type == "number":
            row.value_number = float(raw_value)
        elif field_type == "date":
            row.value_datetime = _parse_datetime(raw_value)
        elif field_type == "checkbox":
            row.value_json = {"value": bool(raw_value)}
        elif field_type == "reference":
            target = await self.resolve_node(workspace_id=node.workspace_id, ref=str(raw_value))
            row.target_node_id = target.id
        else:
            row.value_text = str(raw_value)
        row.updated_by = user_id
        row.updated_at = _now()

    async def create_nodes_from_outline(
        self,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID | None,
        parent: KnowledgeNode,
        outline_text: str,
        project_id: uuid.UUID | None = None,
    ) -> list[KnowledgeNode]:
        parsed_lines = _parse_outline_text(outline_text)
        stack: dict[int, KnowledgeNode] = {-1: parent}
        created: list[KnowledgeNode] = []
        for parsed in parsed_lines:
            parent_depth = parsed.depth - 1
            while parent_depth not in stack and parent_depth >= -1:
                parent_depth -= 1
            parent_node = stack.get(parent_depth, parent)
            node = await self.create_node(
                workspace_id=workspace_id,
                user_id=user_id,
                parent=parent_node,
                project_id=project_id or parent.project_id,
                title=parsed.title,
            )
            created.append(node)
            stack[parsed.depth] = node
            for deeper in [depth for depth in stack if depth > parsed.depth]:
                stack.pop(deeper, None)
            for tag_name in parsed.tags:
                tag = await self.resolve_supertag(workspace_id=workspace_id, tag=tag_name, create=True)
                await self.add_tag(node=node, tag=tag, user_id=user_id)
            if parsed.fields:
                await self.set_fields(node=node, values=parsed.fields, user_id=user_id)
        return created

    async def move_node(
        self,
        *,
        node: KnowledgeNode,
        new_parent: KnowledgeNode,
        user_id: uuid.UUID | None,
        leave_reference: bool = False,
    ) -> KnowledgeNode:
        old_parent_id = node.parent_id
        if node.id == new_parent.id:
            raise ValueError("node cannot be moved under itself")
        # 循環防止: new_parent が node のサブツリー内なら拒否する。
        # new_parent から親を根まで遡り、node.id に当たれば子孫への移動＝循環。
        ancestor = new_parent
        seen: set[uuid.UUID] = set()
        while ancestor is not None:
            if ancestor.id == node.id:
                raise ValueError("node cannot be moved under its own descendant")
            if ancestor.id in seen or ancestor.parent_id is None:
                break
            seen.add(ancestor.id)
            ancestor = await self.session.get(KnowledgeNode, ancestor.parent_id)
        node.parent_id = new_parent.id
        node.root_page_id = new_parent.root_page_id or new_parent.id
        node.project_id = new_parent.project_id or node.project_id
        node.sort_order = await self._next_sort_order(new_parent.id, node.workspace_id)
        node.updated_by = user_id
        node.updated_at = _now()
        # 子孫の root_page_id を新しいルートページへ伝播する（不変条件: 検索index root_page 更新）。
        await self._propagate_root_page(node)
        if leave_reference and old_parent_id is not None:
            exists = await self.session.execute(
                select(KnowledgeNodePlacement.id)
                .where(
                    KnowledgeNodePlacement.node_id == node.id,
                    KnowledgeNodePlacement.parent_node_id == old_parent_id,
                )
                .limit(1)
            )
            if exists.scalar_one_or_none() is None:
                self.session.add(
                    KnowledgeNodePlacement(
                        node_id=node.id,
                        parent_node_id=old_parent_id,
                        sort_order=node.sort_order,
                        created_by=user_id,
                    )
                )
        await self.record_node_change(
            node,
            user_id,
            "nodeを参照を残して移動" if leave_reference else "nodeを移動",
        )
        await self.session.flush()
        return node

    async def _propagate_root_page(self, root_node: KnowledgeNode) -> None:
        """root_node 配下の全子孫の root_page_id を root_node のルートページへ揃える。"""
        new_root = root_node.root_page_id or root_node.id
        result = await self.session.execute(
            select(KnowledgeNode).where(KnowledgeNode.workspace_id == root_node.workspace_id)
        )
        children: dict[uuid.UUID | None, list[KnowledgeNode]] = {}
        for n in result.scalars().all():
            children.setdefault(n.parent_id, []).append(n)
        stack = list(children.get(root_node.id, []))
        seen: set[uuid.UUID] = {root_node.id}
        while stack:
            node = stack.pop()
            if node.id in seen:
                continue
            seen.add(node.id)
            node.root_page_id = new_root
            stack.extend(children.get(node.id, []))

    async def archive_node(self, *, node: KnowledgeNode, user_id: uuid.UUID | None) -> KnowledgeNode:
        node.archived_at = _now()
        node.updated_by = user_id
        node.updated_at = _now()
        await self.record_node_change(node, user_id, "nodeをアーカイブ")
        # アーカイブ時は連携タスクを unlink する（不変条件 1.6: nodes/archive → task unlink）。
        await self._unlink_bound_task(node=node, user_id=user_id)
        await self.session.flush()
        return node

    async def archive_subtree(
        self,
        *,
        root: KnowledgeNode,
        user_id: uuid.UUID | None,
    ) -> list[KnowledgeNode]:
        """root以下を全てarchiveし、activeな孤児・検索結果を残さない。"""
        result = await self.session.execute(
            select(KnowledgeNode).where(KnowledgeNode.workspace_id == root.workspace_id)
        )
        children: dict[uuid.UUID | None, list[KnowledgeNode]] = {}
        for node in result.scalars().all():
            children.setdefault(node.parent_id, []).append(node)
        ordered = [root]
        cursor = 0
        seen: set[uuid.UUID] = set()
        while cursor < len(ordered):
            node = ordered[cursor]
            cursor += 1
            if node.id in seen:
                continue
            seen.add(node.id)
            ordered.extend(children.get(node.id, []))
        archived: list[KnowledgeNode] = []
        for node in ordered:
            if node.archived_at is None:
                await self.archive_node(node=node, user_id=user_id)
                archived.append(node)
        return archived

    async def set_field_by_id(
        self,
        *,
        node: KnowledgeNode,
        field_id: uuid.UUID,
        value: Any,
        user_id: uuid.UUID | None,
    ) -> dict[str, str]:
        """push/REST の field_value 更新（field_id 直指定）。

        field を id で取得し、ノードのタグ定義に属することを検証したうえで
        ``{field.name: value}`` を組んで既存 ``set_fields`` に委譲する
        （task 系 system_key の連携タスク更新・型別格納をそのまま再利用）。
        """
        field = await self.session.get(KnowledgeField, field_id)
        if field is None or field.workspace_id != node.workspace_id:
            raise ValueError(f"field not found: {field_id}")
        # set_fields が resolve_node_fields でノードのタグ定義に属すかを検証する。
        return await self.set_fields(node=node, values={field.name: value}, user_id=user_id)

    async def _ensure_system_node(
        self,
        *,
        workspace_id: uuid.UUID,
        title: str,
        parent_id: uuid.UUID | None,
        sort_order: float,
        user_id: uuid.UUID | None,
        node_type: str = "system",
    ) -> KnowledgeNode:
        """Web `ensureSystemNode` 相当。title+parent で一意な祖先ノードを ensure する。"""
        conditions: list[Any] = [
            KnowledgeNode.workspace_id == workspace_id,
            KnowledgeNode.title == title,
            KnowledgeNode.archived_at.is_(None),
        ]
        if parent_id is None:
            conditions.append(KnowledgeNode.parent_id.is_(None))
        else:
            conditions.append(KnowledgeNode.parent_id == parent_id)
        result = await self.session.execute(
            select(KnowledgeNode).where(*conditions).order_by(KnowledgeNode.created_at).limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing
        # Web は rootPageId=parentId（直近の親）を採用するため、それを踏襲する。
        node = KnowledgeNode(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            parent_id=parent_id,
            root_page_id=parent_id,
            title=title[:500],
            body_text=_title_mirror(title),
            body_json={"inline": [{"type": "text", "text": title}]},
            node_type=node_type,
            sort_order=sort_order,
            created_by=user_id,
            updated_by=user_id,
        )
        self.session.add(node)
        await self.session.flush()
        await self.record_node_change(node, user_id, "systemノードを作成")
        return node

    async def ensure_daily_page(
        self,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID | None,
        day: date,
    ) -> tuple[KnowledgeNode, KnowledgeSupertag, list[KnowledgeNodeSupertag]]:
        """Web `today/route.ts` と同一階層で Day ノードを ensure する。

        Daily notes > <year> > Week NN > Day の祖先を作成/正規化し、Day タグ付与と
        day_date 設定を行う。戻り値は (dayノード, Dayタグ, node_supertags)。
        """
        day_iso = day.isoformat()
        # Day タグ（resolve_supertag は Day を作成/取得できる）。
        day_tag = await self.resolve_supertag(workspace_id=workspace_id, tag="Day", create=True)

        iso_year, iso_week, _ = day.isocalendar()
        daily_root = await self._ensure_system_node(
            workspace_id=workspace_id, title="Daily notes", parent_id=None, sort_order=10, user_id=user_id
        )
        year_root = await self._ensure_system_node(
            workspace_id=workspace_id, title=str(day.year), parent_id=daily_root.id,
            sort_order=float(day.year), user_id=user_id,
        )
        week_root = await self._ensure_system_node(
            workspace_id=workspace_id, title=f"Week {iso_week:02d}", parent_id=year_root.id,
            sort_order=float(iso_week), user_id=user_id,
        )

        existing_result = await self.session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.workspace_id == workspace_id,
                KnowledgeNode.day_date == day,
                KnowledgeNode.archived_at.is_(None),
            )
            .order_by(KnowledgeNode.created_at)
            .limit(1)
        )
        day_node = existing_result.scalar_one_or_none()
        if day_node is not None:
            if day_node.parent_id != week_root.id or day_node.root_page_id != daily_root.id:
                day_node.parent_id = week_root.id
                day_node.root_page_id = daily_root.id
                day_node.updated_by = user_id
                day_node.updated_at = _now()
                await self.session.flush()
        else:
            title = f"{day.year}年{day.month}月{day.day}日"
            day_node = await self.create_node(
                workspace_id=workspace_id,
                user_id=user_id,
                title=title,
                parent=week_root,
                node_type="day",
                day_date=day,
            )
            # create_node は root_page を親(week)基準にするため Web に合わせて Daily notes へ寄せる。
            day_node.root_page_id = daily_root.id
            await self.session.flush()

        await self.add_tag(node=day_node, tag=day_tag, user_id=user_id)
        tags_result = await self.session.execute(
            select(KnowledgeNodeSupertag).where(KnowledgeNodeSupertag.node_id == day_node.id)
        )
        node_supertags = list(tags_result.scalars().all())
        return day_node, day_tag, node_supertags

    async def ensure_child_sections(
        self,
        *,
        parent: KnowledgeNode,
        titles: Iterable[str],
        user_id: uuid.UUID | None,
        body_by_title: dict[str, str] | None = None,
    ) -> list[KnowledgeNode]:
        existing_result = await self.session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.workspace_id == parent.workspace_id,
                KnowledgeNode.parent_id == parent.id,
                KnowledgeNode.archived_at.is_(None),
            )
            .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
        )
        existing_by_title = {node.title: node for node in existing_result.scalars().all()}
        sections: list[KnowledgeNode] = []
        for title in titles:
            section = existing_by_title.get(title)
            if section is None:
                section = await self.create_node(
                    workspace_id=parent.workspace_id,
                    user_id=user_id,
                    parent=parent,
                    project_id=parent.project_id,
                    title=title,
                )
                initial_body = (body_by_title or {}).get(title, "").strip()
                if initial_body:
                    content_parent = await self._ensure_section_content_container(
                        section=section,
                        user_id=user_id,
                    )
                    await self.create_nodes_from_outline(
                        workspace_id=parent.workspace_id,
                        user_id=user_id,
                        parent=content_parent,
                        outline_text=initial_body,
                        project_id=parent.project_id,
                    )
            elif (section.body_text or "").strip() not in {"", _title_mirror(section.title)}:
                legacy_body = section.body_text
                content_parent = await self._ensure_section_content_container(
                    section=section,
                    user_id=user_id,
                )
                await self.create_nodes_from_outline(
                    workspace_id=parent.workspace_id,
                    user_id=user_id,
                    parent=content_parent,
                    outline_text=legacy_body,
                    project_id=parent.project_id,
                )
                section.body_text = _title_mirror(section.title)
            sections.append(section)
        return sections

    async def _ensure_section_content_container(
        self,
        *,
        section: KnowledgeNode,
        user_id: uuid.UUID | None,
    ) -> KnowledgeNode:
        system_key = f"docs_section_content:{section.id}"
        result = await self.session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.workspace_id == section.workspace_id,
                KnowledgeNode.system_key == system_key,
                KnowledgeNode.archived_at.is_(None),
            )
            .limit(1)
        )
        container = result.scalar_one_or_none()
        if container is not None:
            return container
        return await self.create_node(
            workspace_id=section.workspace_id,
            user_id=user_id,
            parent=section,
            project_id=section.project_id,
            title="内容",
            system_key=system_key,
            body_json={"format": "doc_block", "block_type": "content_container"},
        )

    async def append_to_section(
        self,
        *,
        parent: KnowledgeNode,
        section_title: str,
        text: str,
        operation: str,
        user_id: uuid.UUID | None,
    ) -> KnowledgeNode:
        sections = await self.ensure_child_sections(
            parent=parent,
            titles=[section_title],
            user_id=user_id,
        )
        section = sections[0]
        body = str(text or "").strip()
        if not body:
            return section
        content_parent = await self._ensure_section_content_container(
            section=section,
            user_id=user_id,
        )
        if (section.body_text or "").strip() not in {"", _title_mirror(section.title)}:
            legacy_body = section.body_text
            await self.create_nodes_from_outline(
                workspace_id=parent.workspace_id,
                user_id=user_id,
                parent=content_parent,
                outline_text=legacy_body,
                project_id=parent.project_id,
            )
            section.body_text = _title_mirror(section.title)
        if str(operation or "append").casefold() == "replace":
            content_parent.system_key = f"docs_section_content_archived:{section.id}:{content_parent.id}"
            await self.archive_subtree(
                root=content_parent,
                user_id=user_id,
            )
            content_parent = await self._ensure_section_content_container(
                section=section,
                user_id=user_id,
            )
        await self.create_nodes_from_outline(
            workspace_id=parent.workspace_id,
            user_id=user_id,
            parent=content_parent,
            outline_text=body,
            project_id=parent.project_id,
        )
        section.body_text = _title_mirror(section.title)
        section.updated_by = user_id
        section.updated_at = _now()
        await self.record_node_change(section, user_id, f"{section_title}を更新")
        await self.session.flush()
        return section

    async def search(
        self,
        *,
        workspace_id: uuid.UUID,
        query: str = "",
        project_id: uuid.UUID | None = None,
        tag: str = "",
        limit: int = 20,
    ) -> list[KnowledgeNode]:
        stmt = (
            select(KnowledgeNode)
            .join(KnowledgeSearchIndex, KnowledgeSearchIndex.node_id == KnowledgeNode.id)
            .where(KnowledgeNode.workspace_id == workspace_id, KnowledgeNode.archived_at.is_(None))
        )
        if project_id is not None:
            stmt = stmt.where(KnowledgeNode.project_id == project_id)
        if query.strip():
            like_term = f"%{query.strip()}%"
            email_body_match = (
                select(KnowledgeFieldValue.node_id)
                .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
                .where(
                    KnowledgeFieldValue.node_id == KnowledgeNode.id,
                    KnowledgeField.system_key == "email_body",
                    KnowledgeFieldValue.value_text.ilike(like_term),
                )
                .exists()
            )
            stmt = stmt.where(
                or_(
                    KnowledgeSearchIndex.title_text.ilike(like_term),
                    KnowledgeSearchIndex.body_text_plain.ilike(like_term),
                    email_body_match,
                )
            )
        if tag.strip():
            tag_row = await self.resolve_supertag(workspace_id=workspace_id, tag=tag, create=False)
            stmt = stmt.join(
                KnowledgeNodeSupertag,
                KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
            ).where(KnowledgeNodeSupertag.supertag_id == tag_row.id)
        stmt = stmt.order_by(KnowledgeNode.updated_at.desc()).limit(max(1, min(int(limit or 20), 100)))
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def outline_lines(
        self,
        *,
        root: KnowledgeNode,
        depth: int = 3,
        node_filter: Callable[[KnowledgeNode], Awaitable[bool]] | None = None,
    ) -> list[str]:
        max_depth = max(0, min(int(depth or 3), 8))
        scope_condition = (
            KnowledgeNode.project_id == root.project_id
            if root.project_id is not None
            else or_(
                KnowledgeNode.id == root.id,
                KnowledgeNode.root_page_id == root.id,
                KnowledgeNode.parent_id == root.id,
            )
        )
        result = await self.session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.workspace_id == root.workspace_id,
                scope_condition,
                KnowledgeNode.archived_at.is_(None),
            )
            .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
            .limit(500)
        )
        nodes = list(result.scalars().unique().all())
        children: dict[uuid.UUID | None, list[KnowledgeNode]] = {}
        for node in nodes:
            children.setdefault(node.parent_id, []).append(node)

        tag_rows = await self.session.execute(
            select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
            .join(KnowledgeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(KnowledgeNodeSupertag.node_id.in_([node.id for node in nodes]))
        )
        tags_by_node: dict[uuid.UUID, list[str]] = {}
        for node_id, tag_name in tag_rows.all():
            tags_by_node.setdefault(node_id, []).append(tag_name)

        lines: list[str] = []

        async def visit(node: KnowledgeNode, current_depth: int) -> None:
            if current_depth > max_depth:
                return
            if node_filter is not None and not await node_filter(node):
                return
            tags = " ".join(f"#{name}" for name in tags_by_node.get(node.id, []))
            suffix = f" {tags}" if tags else ""
            indent = "\t" * current_depth
            lines.append(f"{indent}{_short_id(node.id)} {node.title}{suffix}")
            for child in children.get(node.id, []):
                await visit(child, current_depth + 1)

        await visit(root, 0)
        return lines

    async def format_search_results(self, nodes: list[KnowledgeNode]) -> str:
        if not nodes:
            return "No Docs nodes found."
        node_ids = [node.id for node in nodes]
        tag_rows = await self.session.execute(
            select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
            .join(KnowledgeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(KnowledgeNodeSupertag.node_id.in_(node_ids))
        )
        tags_by_node: dict[uuid.UUID, list[str]] = {}
        for node_id, tag_name in tag_rows.all():
            tags_by_node.setdefault(node_id, []).append(tag_name)
        parents_by_node = await self._parent_titles(nodes)
        lines = []
        for node in nodes:
            tags = " ".join(f"#{name}" for name in tags_by_node.get(node.id, [])[:5])
            project = f" project={_short_id(node.project_id)}" if node.project_id else ""
            tag_text = f" {tags}" if tags else ""
            parent_title = parents_by_node.get(node.id)
            parent_text = f" ⤷ {parent_title}" if parent_title else ""
            lines.append(f"{_short_id(node.id)} | {node.title}{tag_text}{project}{parent_text}")
        return "\n".join(lines)

    async def _parent_titles(self, nodes: list[KnowledgeNode]) -> dict[uuid.UUID, str]:
        """Return the immediate parent title for each node, batched."""
        parent_ids = {node.parent_id for node in nodes if node.parent_id is not None}
        if not parent_ids:
            return {}
        result = await self.session.execute(
            select(KnowledgeNode.id, KnowledgeNode.title).where(KnowledgeNode.id.in_(parent_ids))
        )
        title_by_id = {row_id: title for row_id, title in result.all()}
        return {
            node.id: title_by_id[node.parent_id]
            for node in nodes
            if node.parent_id in title_by_id
        }

    async def ancestor_titles(self, node: KnowledgeNode, max_depth: int = 8) -> list[str]:
        """Return ancestor titles from the root down to (but excluding) node."""
        titles: list[str] = []
        seen: set[uuid.UUID] = {node.id}
        current = node
        for _ in range(max_depth):
            parent_id = current.parent_id
            if parent_id is None or parent_id in seen:
                break
            seen.add(parent_id)
            parent = await self.session.get(KnowledgeNode, parent_id)
            if parent is None:
                break
            titles.append(parent.title or "")
            current = parent
        return list(reversed(titles))

    async def get_backlinks(self, node: KnowledgeNode, limit: int = 50) -> list[KnowledgeNode]:
        """Return nodes that reference this node via inline `[[...]]` edges."""
        result = await self.session.execute(
            select(KnowledgeNode)
            .join(KnowledgeEdge, KnowledgeEdge.source_node_id == KnowledgeNode.id)
            .where(
                KnowledgeEdge.target_node_id == node.id,
                KnowledgeEdge.relation_type.in_(["inline_ref", "references"]),
                KnowledgeNode.archived_at.is_(None),
            )
            .order_by(KnowledgeNode.updated_at.desc())
            .limit(max(1, min(int(limit or 50), 200)))
        )
        return list(result.scalars().unique().all())

    def _format_field_value(self, field: KnowledgeField, value: KnowledgeFieldValue) -> str:
        field_type = str(field.field_type or "text")
        if field_type == "number" and value.value_number is not None:
            number = value.value_number
            return str(int(number)) if float(number).is_integer() else str(number)
        if field_type == "date" and value.value_datetime is not None:
            return value.value_datetime.isoformat()
        if field_type == "checkbox" and isinstance(value.value_json, dict):
            return "true" if value.value_json.get("value") else "false"
        if field_type == "reference" and value.target_node_id is not None:
            return f"[[node:{value.target_node_id}]]"
        if value.value_text is not None:
            return value.value_text
        if value.value_json is not None:
            return str(value.value_json)
        return ""

    async def _get_bound_task(self, node: KnowledgeNode) -> Task | None:
        result = await self.session.execute(
            select(Task).where(Task.knowledge_node_id == node.id, Task.deleted_at.is_(None)).limit(1)
        )
        return result.scalar_one_or_none()

    async def get_node_field_values(self, node: KnowledgeNode) -> dict[str, str]:
        """Return current field name -> display value for a node.

        Includes Docs-native field values (``KnowledgeFieldValue``) and, when the
        node is bound to a task via ``#Task``, the current task-system field values
        (status/due/start/priority) which live on the task, not on the node.
        """
        result = await self.session.execute(
            select(KnowledgeField, KnowledgeFieldValue)
            .join(KnowledgeFieldValue, KnowledgeFieldValue.field_id == KnowledgeField.id)
            .where(KnowledgeFieldValue.node_id == node.id)
            .order_by(KnowledgeField.sort_order, KnowledgeField.created_at)
        )
        values: dict[str, str] = {}
        for field, value in result.all():
            rendered = self._format_field_value(field, value)
            if rendered != "":
                values[field.name] = rendered

        fields_by_ref = await self.resolve_node_fields(node)
        if any(key in fields_by_ref for key in TASK_FIELD_TO_TASK_UPDATE):
            task = await self._get_bound_task(node)
            if task is not None:
                for system_key, task_attr in TASK_FIELD_TO_TASK_UPDATE.items():
                    field = fields_by_ref.get(system_key)
                    if field is None:
                        continue
                    raw = getattr(task, task_attr, None)
                    if raw in (None, ""):
                        continue
                    values[field.name] = raw.isoformat() if isinstance(raw, datetime) else str(raw)
        return values

    async def query_nodes(
        self,
        *,
        workspace_id: uuid.UUID,
        tags: list[str] | None = None,
        text: str = "",
        project_id: uuid.UUID | None = None,
        field_filters: dict[str, str] | None = None,
        limit: int = 50,
    ) -> list[KnowledgeNode]:
        """Structured query: AND over tags, optional field equality, text ILIKE."""
        stmt = select(KnowledgeNode).where(
            KnowledgeNode.workspace_id == workspace_id,
            KnowledgeNode.archived_at.is_(None),
        )
        if project_id is not None:
            stmt = stmt.where(KnowledgeNode.project_id == project_id)
        if text.strip():
            stmt = stmt.where(KnowledgeNode.title.ilike(f"%{text.strip()}%"))
        for tag_name in tags or []:
            tag_name = str(tag_name).strip().lstrip("#")
            if not tag_name:
                continue
            tag_row = await self.resolve_supertag(
                workspace_id=workspace_id, tag=tag_name, create=False
            )
            tag_exists = (
                select(KnowledgeNodeSupertag.node_id)
                .where(
                    KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
                    KnowledgeNodeSupertag.supertag_id == tag_row.id,
                )
                .exists()
            )
            stmt = stmt.where(tag_exists)
        for field_name, expected in (field_filters or {}).items():
            field_name = str(field_name).strip()
            if not field_name:
                continue
            expected_text = str(expected).strip()
            field_match = (
                select(KnowledgeFieldValue.node_id)
                .join(KnowledgeField, KnowledgeField.id == KnowledgeFieldValue.field_id)
                .where(
                    KnowledgeFieldValue.node_id == KnowledgeNode.id,
                    or_(
                        func.lower(KnowledgeField.name) == field_name.casefold(),
                        func.lower(KnowledgeField.system_key) == field_name.casefold(),
                    ),
                    or_(
                        func.lower(func.coalesce(KnowledgeFieldValue.value_text, "")) == expected_text.casefold(),
                        cast(KnowledgeFieldValue.value_number, String) == expected_text,
                        cast(KnowledgeFieldValue.target_node_id, String).ilike(f"{expected_text}%"),
                    ),
                )
                .exists()
            )
            stmt = stmt.where(field_match)
        stmt = stmt.order_by(KnowledgeNode.updated_at.desc()).limit(
            max(1, min(int(limit or 50), 200))
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())
