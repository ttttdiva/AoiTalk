"""Shared service layer for AoiTalk Docs graph operations."""

from __future__ import annotations

import re
import uuid
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
        lines.append(ParsedOutlineLine(depth=depth, title=title[:500], tags=tags, fields=fields))
    return lines


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
            today = date.today()
            result = await self.session.execute(
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.workspace_id == workspace_id,
                    KnowledgeNode.day_date == today,
                    KnowledgeNode.archived_at.is_(None),
                )
                .order_by(KnowledgeNode.created_at)
                .limit(1)
            )
            node = result.scalar_one_or_none()
            if node is None:
                node = KnowledgeNode(
                    workspace_id=workspace_id,
                    title=today.isoformat(),
                    body_text="",
                    body_json={"format": "day"},
                    node_type="node",
                    day_date=today,
                    sort_order=0,
                )
                self.session.add(node)
                await self.session.flush()
                await self.record_node_change(node, None, "Day nodeを作成")
            return node

        parsed_uuid = _coerce_uuid(text) if _UUID_RE.match(text) else None
        if parsed_uuid is not None:
            node = await self.session.get(KnowledgeNode, parsed_uuid)
            if node and node.workspace_id == workspace_id and (allow_archived or node.archived_at is None):
                return node
            raise ValueError(f"node not found: {text}")

        if re.fullmatch(r"[0-9a-fA-F]{8,12}", text):
            conditions = [
                KnowledgeNode.workspace_id == workspace_id,
                cast(KnowledgeNode.id, String).ilike(f"{text}%"),
            ]
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
                source_refs_json=[],
                created_by=user_id,
            )
        )

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
    ) -> KnowledgeNode:
        root_page_id = None
        if parent is not None:
            root_page_id = parent.root_page_id or parent.id
            if project_id is None:
                project_id = parent.project_id
        node = KnowledgeNode(
            workspace_id=workspace_id,
            parent_id=parent.id if parent else None,
            root_page_id=root_page_id,
            project_id=project_id,
            title=(title or "Untitled").strip()[:500],
            body_text=body_text or "",
            body_json=body_json or {},
            node_type=node_type,
            sort_order=sort_order
            if sort_order is not None
            else await self._next_sort_order(parent.id if parent else None, workspace_id),
            created_by=user_id,
            updated_by=user_id,
        )
        self.session.add(node)
        await self.session.flush()
        await self.record_node_change(node, user_id, "nodeを作成")
        return node

    async def update_node(
        self,
        *,
        node: KnowledgeNode,
        user_id: uuid.UUID | None,
        title: str | None = None,
        description: str | None = None,
        body_text: str | None = None,
    ) -> KnowledgeNode:
        if title is not None and title.strip():
            node.title = title.strip()[:500]
            await self._sync_bound_task_title(node=node, user_id=user_id)
        if description is not None:
            node.description = str(description)[:200000]
        if body_text is not None:
            node.body_text = str(body_text)[:200000]
        node.updated_by = user_id
        node.updated_at = _now()
        await self.record_node_change(node, user_id, "nodeを更新")
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
        node.parent_id = new_parent.id
        node.root_page_id = new_parent.root_page_id or new_parent.id
        node.project_id = new_parent.project_id or node.project_id
        node.sort_order = await self._next_sort_order(new_parent.id, node.workspace_id)
        node.updated_by = user_id
        node.updated_at = _now()
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

    async def archive_node(self, *, node: KnowledgeNode, user_id: uuid.UUID | None) -> KnowledgeNode:
        node.archived_at = _now()
        node.updated_by = user_id
        node.updated_at = _now()
        await self.record_node_change(node, user_id, "nodeをアーカイブ")
        await self.session.flush()
        return node

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
                    body_text=(body_by_title or {}).get(title, ""),
                )
            sections.append(section)
        return sections

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
        if str(operation or "append").casefold() == "replace":
            section.body_text = body
        else:
            current = (section.body_text or "").rstrip()
            section.body_text = f"{current}\n\n{body}" if current else body
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
            stmt = stmt.where(
                or_(
                    KnowledgeSearchIndex.title_text.ilike(like_term),
                    KnowledgeSearchIndex.body_text_plain.ilike(like_term),
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

    async def outline_lines(self, *, root: KnowledgeNode, depth: int = 3) -> list[str]:
        max_depth = max(0, min(int(depth or 3), 8))
        result = await self.session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.workspace_id == root.workspace_id,
                or_(KnowledgeNode.id == root.id, KnowledgeNode.root_page_id == root.id, KnowledgeNode.parent_id == root.id),
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

        def visit(node: KnowledgeNode, current_depth: int) -> None:
            if current_depth > max_depth:
                return
            tags = " ".join(f"#{name}" for name in tags_by_node.get(node.id, []))
            suffix = f" {tags}" if tags else ""
            indent = "\t" * current_depth
            lines.append(f"{indent}{_short_id(node.id)} {node.title}{suffix}")
            for child in children.get(node.id, []):
                visit(child, current_depth + 1)

        visit(root, 0)
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
        lines = []
        for node in nodes:
            tags = " ".join(f"#{name}" for name in tags_by_node.get(node.id, [])[:5])
            project = f" project={_short_id(node.project_id)}" if node.project_id else ""
            tag_text = f" {tags}" if tags else ""
            lines.append(f"{_short_id(node.id)} | {node.title}{tag_text}{project}")
        return "\n".join(lines)
