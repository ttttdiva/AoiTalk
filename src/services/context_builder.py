"""Build compact runtime context blocks for LLM prompts."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from sqlalchemy import or_, select

from ..memory.database import get_db_session
from ..memory.models import ConversationSession, Task
from .context_memory_service import ContextMemoryService
from .project_context import (
    ProjectContextResolver,
    format_minimal_project_context_for_chat_prompt,
    format_project_context_for_chat_prompt,
)
from .project_context_pack_service import ProjectContextPackService

logger = logging.getLogger(__name__)


def _coerce_uuid(value: Optional[str]) -> Optional[uuid.UUID]:
    if not value:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _clip_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else ""


def _heading_outline(text: str, *, limit: int = 20) -> list[str]:
    headings: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        title = stripped.lstrip("#").strip()
        if title:
            headings.append(title)
        if len(headings) >= limit:
            break
    return headings


def _field_value_text(value: Any) -> str:
    if getattr(value, "target_node_id", None):
        return f"@docs:{value.target_node_id}"
    if getattr(value, "value_text", None):
        return str(value.value_text)
    if getattr(value, "value_number", None) is not None:
        return str(value.value_number)
    if getattr(value, "value_datetime", None) is not None:
        return _iso(value.value_datetime)
    raw = getattr(value, "value_json", None)
    if raw is None:
        return ""
    if isinstance(raw, dict):
        for key in ("value", "text", "label", "id"):
            if raw.get(key) not in (None, ""):
                return str(raw[key])
    return str(raw)


@dataclass
class ContextBundle:
    memory_context_block: str = ""
    project_context_block: str = ""
    project_information_block: str = ""
    agent_memory_block: str = ""
    project_pack_block: str = ""
    task_context_block: str = ""
    session_context_block: str = ""
    debug: Dict[str, Any] = field(default_factory=dict)
    max_chars: int = 12000

    def render_with_trace(self, max_chars: Optional[int] = None) -> tuple[str, list[dict[str, Any]]]:
        limit = max_chars or self.max_chars
        blocks = [
            ("project_context", "Project context", "ContextBundle.project_context_block", self.project_context_block),
            ("project_information", "Project information / Docs", "ContextBundle.project_information_block", self.project_information_block),
            ("agent_memory", "Agent Memory", "ContextBundle.agent_memory_block", self.agent_memory_block),
            ("context_memory", "Context memory", "ContextBundle.memory_context_block", self.memory_context_block),
            ("project_context_pack", "Project context pack", "ContextBundle.project_pack_block", self.project_pack_block),
            ("active_task_context", "Active task context", "ContextBundle.task_context_block", self.task_context_block),
            ("session_summary", "Session summary", "ContextBundle.session_context_block", self.session_context_block),
        ]
        seen: set[str] = set()
        rendered: list[str] = []
        trace: list[dict[str, Any]] = []
        total = 0
        for category, label, source, block in blocks:
            block = (block or "").strip()
            if not block:
                continue
            if block in seen:
                trace.append({"category": category, "label": label, "source": source, "text": "", "status": "deferred", "preview": "重複のため未送信"})
                continue
            seen.add(block)
            next_total = total + len(block) + (2 if rendered else 0)
            if next_total > limit:
                remaining = limit - total - (2 if rendered else 0)
                if remaining > 80:
                    clipped = _clip_text(block, remaining)
                    rendered.append(clipped)
                    trace.append({"category": category, "label": label, "source": source, "text": clipped, "status": "active", "preview": "上限に合わせて切り詰めて送信"})
                else:
                    trace.append({"category": category, "label": label, "source": source, "text": "", "status": "deferred", "preview": "コンテキスト予算超過のため未送信"})
                break
            rendered.append(block)
            trace.append({"category": category, "label": label, "source": source, "text": block, "status": "active", "preview": "モデルへ送信済み"})
            total = next_total
        return "\n\n".join(rendered), trace

    def render_for_prompt(self, max_chars: Optional[int] = None) -> str:
        return self.render_with_trace(max_chars)[0]


class ContextBuilder:
    """Collect existing and scoped context into one prompt block."""

    def __init__(
        self,
        *,
        context_memory_service: Optional[ContextMemoryService] = None,
        project_context_pack_service: Optional[ProjectContextPackService] = None,
        project_context_resolver: Optional[ProjectContextResolver] = None,
    ):
        self.context_memory_service = context_memory_service or ContextMemoryService()
        self.project_context_pack_service = (
            project_context_pack_service or ProjectContextPackService()
        )
        self.project_context_resolver = project_context_resolver or ProjectContextResolver()

    async def build_context(
        self,
        *,
        user_id: str,
        message: str,
        project_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        max_chars: int = 12000,
        project_context: Optional[dict[str, Any]] = None,
        include_project_context: bool = True,
        include_project_information: bool = True,
        include_agent_memory: bool = True,
        include_project_pack: bool = True,
        include_task_context: bool = True,
        project_context_mode: str = "full",
    ) -> ContextBundle:
        debug: Dict[str, Any] = {
            "user_id": user_id,
            "project_id": project_id,
            "task_id": task_id,
            "session_id": session_id,
            "errors": {},
        }
        bundle = ContextBundle(max_chars=max_chars, debug=debug)

        if session_id and (not user_id or user_id == "default_user"):
            try:
                resolved_user_id = await self._resolve_session_user_id(session_id)
            except Exception as exc:
                logger.warning("[ContextBuilder] session user lookup failed: %s", exc)
                debug["errors"]["session_user"] = str(exc)
            else:
                if resolved_user_id and resolved_user_id != user_id:
                    user_id = resolved_user_id
                    debug["user_id"] = user_id
                    debug["user_id_source"] = "conversation_session"

        resolved_project_context = project_context if include_project_context else None
        if include_project_context:
            if resolved_project_context is None and (project_id or session_id):
                try:
                    resolved_project_context = await self.project_context_resolver.resolve_context(
                        project_id=project_id,
                        session_id=session_id,
                    )
                except Exception as exc:
                    logger.warning("[ContextBuilder] project context failed: %s", exc)
                    debug["errors"]["project_context"] = str(exc)

            if resolved_project_context:
                if project_context_mode == "minimal":
                    bundle.project_context_block = (
                        format_minimal_project_context_for_chat_prompt(
                            resolved_project_context
                        )
                    )
                else:
                    bundle.project_context_block = format_project_context_for_chat_prompt(
                        resolved_project_context
                    )
                if not project_id and resolved_project_context.get("id"):
                    project_id = str(resolved_project_context["id"])
                    debug["project_id"] = project_id

        if include_project_context and project_id:
            if include_project_information:
                try:
                    bundle.project_information_block = await self._build_project_information_block(
                        project_id=project_id,
                        user_id=user_id,
                    )
                    debug["project_information_context"] = bool(
                        bundle.project_information_block
                    )
                except Exception as exc:
                    logger.warning("[ContextBuilder] project information failed: %s", exc)
                    debug["errors"]["project_information"] = str(exc)

            if include_project_pack:
                try:
                    bundle.project_pack_block = (
                        await self.project_context_pack_service.render_project_context_pack_for_prompt(
                            project_id
                        )
                    )
                except Exception as exc:
                    logger.warning("[ContextBuilder] project context pack failed: %s", exc)
                    debug["errors"]["project_context_pack"] = str(exc)

        if include_agent_memory and project_id:
            try:
                bundle.agent_memory_block = await self._build_agent_memory_block(
                    project_id=project_id,
                )
                debug["agent_memory_context"] = bool(bundle.agent_memory_block)
            except Exception as exc:
                logger.warning("[ContextBuilder] agent memory failed: %s", exc)
                debug["errors"]["agent_memory"] = str(exc)

        if include_task_context:
            try:
                bundle.task_context_block = await self._build_task_context_block(
                    project_id=project_id if include_project_context else None,
                    task_id=task_id,
                )
            except Exception as exc:
                logger.warning("[ContextBuilder] task context failed: %s", exc)
                debug["errors"]["task_context"] = str(exc)

        try:
            bundle.session_context_block = await self._build_session_context_block(
                session_id
            )
        except Exception as exc:
            logger.warning("[ContextBuilder] session context failed: %s", exc)
            debug["errors"]["session_context"] = str(exc)

        try:
            memories = await self.context_memory_service.get_memories_for_context(
                user_id=user_id,
                project_id=project_id if include_project_context else None,
                task_id=task_id,
                session_id=session_id,
                message=message,
                limit=20,
            )
            bundle.memory_context_block = (
                self.context_memory_service.render_memories_for_prompt(memories)
            )
            debug["context_memory_count"] = len(memories)
        except Exception as exc:
            logger.warning("[ContextBuilder] context memories failed: %s", exc)
            debug["errors"]["context_memories"] = str(exc)

        return bundle

    async def _resolve_session_user_id(self, session_id: Optional[str]) -> Optional[str]:
        session_uuid = _coerce_uuid(session_id)
        if not session_uuid:
            return None
        async with await get_db_session() as session:
            conversation = await session.get(ConversationSession, session_uuid)
            if not conversation:
                return None
            return str(conversation.user_id) if conversation.user_id else None

    async def _build_session_context_block(self, session_id: Optional[str]) -> str:
        session_uuid = _coerce_uuid(session_id)
        if not session_uuid:
            return ""
        async with await get_db_session() as session:
            conversation = await session.get(ConversationSession, session_uuid)
            if not conversation or not conversation.current_summary:
                return ""
            return "## Session Summary\n" + conversation.current_summary.strip()

    async def _build_task_context_block(
        self,
        *,
        project_id: Optional[str],
        task_id: Optional[str],
    ) -> str:
        task_uuid = _coerce_uuid(task_id)
        project_uuid = _coerce_uuid(project_id)
        async with await get_db_session() as session:
            tasks: list[Task] = []
            if task_uuid:
                task = await session.get(Task, task_uuid)
                if task:
                    tasks.append(task)
            elif project_uuid:
                result = await session.execute(
                    select(Task)
                    .where(Task.project_id == project_uuid)
                    .where(Task.deleted_at.is_(None))
                    .where(Task.archived_at.is_(None))
                    .where(Task.status.notin_(["closed", "done", "completed"]))
                    .order_by(Task.priority.desc(), Task.updated_at.desc())
                    .limit(8)
                )
                tasks = list(result.scalars().all())

        if not tasks:
            return ""

        lines = ["## Active Task Context"]
        for task in tasks:
            title = task.title or "(untitled)"
            details = [f"status={task.status}"]
            if task.priority:
                details.append(f"priority={task.priority}")
            if task.end_at:
                details.append(f"end_at={task.end_at.isoformat()}")
            lines.append(f"- {title} ({', '.join(details)})")
            if task.description:
                lines.append(f"  {task.description.strip()[:500]}")
        return "\n".join(lines)

    async def _build_agent_memory_block(
        self,
        *,
        project_id: str,
        agent_memory_chars: int = 4000,
    ) -> str:
        """プロジェクト毎のエージェントメモリ索引ノードのアウトラインを注入する。

        索引ノード（system_key="agent_memory:<project_id>"）を ensure し、
        その直下の子ノード群（1エントリ=1子ノード）を浅いアウトラインで描画する。
        DB接続やensureに失敗してもチャットを壊さず空ブロックへ落とす。
        """
        project_uuid = _coerce_uuid(project_id)
        if not project_uuid:
            return ""

        from ..memory.models import Project
        from .agent_memory_docs import (
            AGENT_MEMORY_AI_INSTRUCTIONS,
            ensure_agent_memory_doc,
            get_agent_memory_doc,
        )

        async with await get_db_session() as session:
            node = await get_agent_memory_doc(session, project_uuid)
            if node is None:
                project = await session.get(Project, project_uuid)
                if project is None:
                    return ""
                node = await ensure_agent_memory_doc(session, project)
                await session.commit()

            node_id = node.id
            node_title = (node.title or "(untitled)").strip()
            # 索引ノードは project_id を持つため DocsGraphService.outline_lines は
            # scope が project_id 一致となり、案件情報等プロジェクト全ノード(LIMIT 500)を
            # 引いてしまう。500超のプロジェクトではメモリの子が取得対象から漏れて静かに
            # 消え得るため、索引ノードの子孫だけを parent_id で辿って直接構築する。
            outline_lines = await self._agent_memory_outline_lines(
                session, node, depth=2
            )

        outline_text = "\n".join(outline_lines).strip()
        truncated = False
        if len(outline_text) > max(1, agent_memory_chars):
            outline_text = _clip_text(outline_text, max(1, agent_memory_chars)).rstrip()
            truncated = True

        lines = [
            "## Agent Memory (project-scoped, agent-maintained)",
            (
                "プロジェクト毎の恒久メモリ（Claude CodeのMEMORY.md相当）。"
                "訂正・導出不能な知見・作業上の嗜好のみをここへ保存する。"
            ),
            f"- Memory Index Node: {node_title} (ref=@docs:{node_id})",
            (
                "- 書込単位: 索引ノード直下に「1エントリ=1子ノード」を docs_create_nodes で追加し、"
                "既存エントリの修正は docs_update_node で行う"
                "（索引ノード本文はタイトルミラー固定のため本文へは書き込まない）。"
            ),
            "- 詳細は各エントリの子ノードにある。必要なら docs_read で該当ノードを読む。",
            "- 上限接近時は古い項目を統合・圧縮する。秘密情報（パスワード/トークン）は保存禁止。",
        ]
        if AGENT_MEMORY_AI_INSTRUCTIONS.strip():
            lines.append(
                "- 保存基準: " + _clip_text(AGENT_MEMORY_AI_INSTRUCTIONS.strip(), 420)
            )
        lines.append("### Memory Entries Outline")
        if outline_text:
            lines.append(outline_text)
            if truncated:
                lines.append("...(truncated; 全量は docs_read で索引ノードを読む)")
        else:
            lines.append("(まだ記憶はありません)")

        return "\n".join(lines)

    async def _agent_memory_outline_lines(
        self,
        session: Any,
        root: Any,
        *,
        depth: int = 2,
    ) -> list[str]:
        """索引ノードのサブツリーだけを浅いアウトラインとして構築する。

        ``DocsGraphService.outline_lines`` はプロジェクト全ノードを引くため、
        ここでは ``parent_id`` を階層ごとに辿って索引ノードの子孫だけを取得し、
        同一フォーマット（``短縮ID タイトル #タグ`` + タブインデント、短縮IDは
        UUID 先頭8hex）で組み立てる。``docs_update_node`` / ``docs_read`` の
        ``resolve_node`` が 8-12hex プレフィックスで解決できる表記を保つ。
        """
        from ..memory.models import (
            KnowledgeNode,
            KnowledgeNodeSupertag,
            KnowledgeSupertag,
        )

        max_depth = max(0, min(int(depth or 2), 8))
        nodes: list[Any] = [root]
        children_map: dict[Any, list[Any]] = {}
        frontier: list[Any] = [root.id]
        current_depth = 0
        while current_depth < max_depth and frontier:
            level_result = await session.execute(
                select(KnowledgeNode)
                .where(
                    KnowledgeNode.workspace_id == root.workspace_id,
                    KnowledgeNode.parent_id.in_(frontier),
                    KnowledgeNode.archived_at.is_(None),
                )
                .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
                .limit(500)
            )
            level_nodes = list(level_result.scalars().unique().all())
            for child in level_nodes:
                children_map.setdefault(child.parent_id, []).append(child)
            nodes.extend(level_nodes)
            frontier = [child.id for child in level_nodes]
            current_depth += 1

        tags_by_node: dict[Any, list[str]] = {}
        if nodes:
            tag_rows = await session.execute(
                select(KnowledgeNodeSupertag.node_id, KnowledgeSupertag.name)
                .join(
                    KnowledgeSupertag,
                    KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id,
                )
                .where(
                    KnowledgeNodeSupertag.node_id.in_([node.id for node in nodes])
                )
            )
            for node_id, tag_name in tag_rows.all():
                tags_by_node.setdefault(node_id, []).append(tag_name)

        lines: list[str] = []

        def visit(node: Any, node_depth: int) -> None:
            if node_depth > max_depth:
                return
            tags = " ".join(f"#{name}" for name in tags_by_node.get(node.id, []))
            suffix = f" {tags}" if tags else ""
            indent = "\t" * node_depth
            lines.append(f"{indent}{str(node.id)[:8]} {node.title}{suffix}")
            for child in children_map.get(node.id, []):
                visit(child, node_depth + 1)

        visit(root, 0)
        return lines

    async def _build_project_information_block(
        self,
        *,
        project_id: str,
        user_id: str,
        max_record_tables: int = 6,
        max_related_nodes: int = 24,
        max_qas: int = 10,
        docs_node_chars: int = 8000,
    ) -> str:
        project_uuid = _coerce_uuid(project_id)
        if not project_uuid:
            return ""

        from ..memory.models import (
            KnowledgeEdge,
            KnowledgeField,
            KnowledgeFieldValue,
            KnowledgeNode,
            KnowledgeNodeSupertag,
            KnowledgeRevision,
            KnowledgeSupertag,
            KnowledgeWorkspace,
            Project,
            ProjectQaEntry,
            RecordTable,
        )

        async with await get_db_session() as session:
            project = await session.get(Project, project_uuid)
            user_uuid = _coerce_uuid(user_id)
            workspace_id = None
            if user_uuid:
                workspace_id = await session.scalar(
                    select(KnowledgeWorkspace.id)
                    .where(KnowledgeWorkspace.owner_user_id == user_uuid)
                    .limit(1)
                )
            tables_result = await session.execute(
                select(RecordTable)
                .where(
                    RecordTable.project_id == project_uuid,
                    RecordTable.deleted_at.is_(None),
                )
                .order_by(RecordTable.sort_order, RecordTable.created_at)
                .limit(max(1, max_record_tables))
            )
            qa_result = await session.execute(
                select(ProjectQaEntry)
                .where(
                    ProjectQaEntry.project_id == project_uuid,
                    ProjectQaEntry.deleted_at.is_(None),
                    ProjectQaEntry.status != "archived",
                    ProjectQaEntry.review_state == "accepted",
                )
                .order_by(ProjectQaEntry.asked_count.desc(), ProjectQaEntry.updated_at.desc())
                .limit(max(1, max_qas))
            )

            tables = list(tables_result.scalars().all())
            qa_entries = list(qa_result.scalars().all())

            canonical_node: Any = None
            if project and project.knowledge_node_id and workspace_id:
                candidate = await session.get(KnowledgeNode, project.knowledge_node_id)
                if (
                    candidate
                    and candidate.workspace_id == workspace_id
                    and candidate.project_id == project_uuid
                    and not candidate.archived_at
                ):
                    canonical_node = candidate

            if canonical_node is None and workspace_id:
                canonical_result = await session.execute(
                    select(KnowledgeNode)
                    .join(
                        KnowledgeNodeSupertag,
                        KnowledgeNodeSupertag.node_id == KnowledgeNode.id,
                    )
                    .join(
                        KnowledgeSupertag,
                        KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id,
                    )
                    .where(
                        KnowledgeNode.workspace_id == workspace_id,
                        KnowledgeNode.project_id == project_uuid,
                        KnowledgeNode.archived_at.is_(None),
                        KnowledgeSupertag.base_type == "project_information",
                    )
                    .order_by(KnowledgeNode.updated_at.desc())
                    .limit(1)
                )
                canonical_node = canonical_result.scalar_one_or_none()

            docs_nodes: list[Any] = []
            if workspace_id:
                related_result = await session.execute(
                    select(KnowledgeNode)
                    .where(
                        KnowledgeNode.workspace_id == workspace_id,
                        KnowledgeNode.project_id == project_uuid,
                        KnowledgeNode.archived_at.is_(None),
                    )
                    .order_by(KnowledgeNode.updated_at.desc())
                    .limit(max(1, max_related_nodes + 1))
                )
                docs_nodes = list(related_result.scalars().all())
            if canonical_node and all(node.id != canonical_node.id for node in docs_nodes):
                docs_nodes.insert(0, canonical_node)

            child_nodes: list[Any] = []
            if canonical_node:
                child_result = await session.execute(
                    select(KnowledgeNode)
                    .where(
                        KnowledgeNode.workspace_id == workspace_id,
                        KnowledgeNode.project_id == project_uuid,
                        KnowledgeNode.archived_at.is_(None),
                    )
                    .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
                    .limit(500)
                )
                child_nodes = list(child_result.scalars().all())

            all_context_nodes = docs_nodes + [
                node for node in child_nodes if all(existing.id != node.id for existing in docs_nodes)
            ]
            docs_tags_by_node: dict[uuid.UUID, list[dict[str, str]]] = {}
            ai_instructions: dict[str, tuple[str, str, str]] = {}
            fields_by_tag: dict[uuid.UUID, list[KnowledgeField]] = {}
            field_values_by_node: dict[uuid.UUID, list[tuple[str, str]]] = {}
            edges: list[Any] = []
            revisions: list[Any] = []
            canonical_outline_lines: list[str] = []
            if docs_nodes:
                node_ids = [node.id for node in all_context_nodes]
                tag_rows = await session.execute(
                    select(
                        KnowledgeNodeSupertag.node_id,
                        KnowledgeSupertag.id,
                        KnowledgeSupertag.name,
                        KnowledgeSupertag.base_type,
                        KnowledgeSupertag.ai_instructions,
                    )
                    .join(
                        KnowledgeSupertag,
                        KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id,
                    )
                    .where(KnowledgeNodeSupertag.node_id.in_(node_ids))
                )
                tag_ids: set[uuid.UUID] = set()
                for node_id, tag_id, tag_name, base_type, instructions in tag_rows.all():
                    tag_ids.add(tag_id)
                    docs_tags_by_node.setdefault(node_id, []).append(
                        {
                            "id": str(tag_id),
                            "name": tag_name,
                            "base_type": base_type or "note",
                        }
                    )
                    if instructions:
                        ai_instructions[tag_name] = (
                            base_type or "note",
                            str(tag_id),
                            instructions.strip(),
                        )

                if tag_ids:
                    fields_result = await session.execute(
                        select(KnowledgeField)
                        .where(KnowledgeField.supertag_id.in_(tag_ids))
                        .order_by(KnowledgeField.sort_order, KnowledgeField.created_at)
                    )
                    for field in fields_result.scalars().all():
                        fields_by_tag.setdefault(field.supertag_id, []).append(field)

                values_result = await session.execute(
                    select(KnowledgeFieldValue, KnowledgeField)
                    .join(KnowledgeField, KnowledgeFieldValue.field_id == KnowledgeField.id)
                    .where(KnowledgeFieldValue.node_id.in_(node_ids))
                    .order_by(KnowledgeField.sort_order, KnowledgeField.created_at)
                )
                for value, field in values_result.all():
                    rendered = _field_value_text(value)
                    if rendered:
                        field_values_by_node.setdefault(value.node_id, []).append(
                            (field.name, rendered)
                        )

                edge_result = await session.execute(
                    select(KnowledgeEdge).where(
                        or_(
                            KnowledgeEdge.source_node_id.in_(node_ids),
                            KnowledgeEdge.target_node_id.in_(node_ids),
                        )
                    ).limit(60)
                )
                edges = list(edge_result.scalars().all())

                if canonical_node:
                    try:
                        from .docs_graph_service import DocsGraphService

                        canonical_outline_lines = await DocsGraphService(
                            session
                        ).outline_lines(root=canonical_node, depth=4)
                    except Exception as exc:
                        logger.warning(
                            "[ContextBuilder] project Docs outline failed: %s",
                            exc,
                        )

                    revision_result = await session.execute(
                        select(KnowledgeRevision)
                        .where(KnowledgeRevision.node_id == canonical_node.id)
                        .order_by(KnowledgeRevision.created_at.desc())
                        .limit(5)
                    )
                    revisions = list(revision_result.scalars().all())

        if not tables and not docs_nodes and not qa_entries:
            return ""

        node_title_by_id = {
            node.id: (node.title or "(untitled)").strip()
            for node in all_context_nodes
        }
        lines = [
            "## Project Information Docs Source of Truth",
            (
                "Use this canonical project Docs context as grounded evidence. Read it before "
                "writing; preserve headings/blocks; update with revision change_summary and "
                "source_refs; route unsupported claims to 要確認 or candidate Q&A."
            ),
        ]
        if project:
            lines.append(
                f"- Project: {project.name} (id={project.id}, slug={project.slug})"
            )

        if canonical_node:
            tags = docs_tags_by_node.get(canonical_node.id, [])
            tag_text = ", ".join(f"#{tag['name']}:{tag['base_type']}" for tag in tags)
            lines.append(
                f"- Canonical Page: {canonical_node.title} (ref=@docs:{canonical_node.id}, updated={_iso(canonical_node.updated_at)})"
            )
            if tag_text:
                lines.append(f"- Canonical Tags: {tag_text}")
            headings = _heading_outline(canonical_node.body_text or "")
            if headings:
                lines.append("- Section Outline: " + " > ".join(headings))
            canonical_fields = field_values_by_node.get(canonical_node.id, [])
            if canonical_fields:
                lines.append("- Canonical Fields:")
                for name, rendered in canonical_fields[:20]:
                    lines.append(f"  - {name}: {_clip_text(rendered, 240)}")
            if canonical_outline_lines:
                lines.append("### Canonical Outline")
                lines.append(_clip_text("\n".join(canonical_outline_lines), docs_node_chars))
            if (canonical_node.body_text or "").strip():
                lines.append("### Canonical Body")
                lines.append(_clip_text((canonical_node.body_text or "").strip(), docs_node_chars))

        typed_nodes = [
            node
            for node in all_context_nodes
            if not canonical_node or node.id != canonical_node.id
        ][:max_related_nodes]
        if typed_nodes:
            lines.append("### Related Typed Docs Nodes")
            for node in typed_nodes:
                tags = docs_tags_by_node.get(node.id, [])
                tag_text = ", ".join(f"#{tag['name']}:{tag['base_type']}" for tag in tags[:5])
                meta = [f"ref=@docs:{node.id}"]
                if tag_text:
                    meta.append(f"tags={tag_text}")
                lines.append(f"- {node.title or '(untitled)'} ({', '.join(meta)})")
                fields = field_values_by_node.get(node.id, [])
                if fields:
                    field_text = "; ".join(
                        f"{name}={_clip_text(rendered, 80)}"
                        for name, rendered in fields[:8]
                    )
                    lines.append(f"  fields: {field_text}")
                body = _clip_text((node.body_text or "").strip(), 320)
                if body:
                    lines.append(f"  body: {body}")

        if ai_instructions:
            lines.append("### Supertag AI Instructions")
            for tag_name, (base_type, tag_id, instructions) in sorted(ai_instructions.items()):
                field_names = [
                    field.name
                    for field in fields_by_tag.get(uuid.UUID(tag_id), [])[:8]
                ]
                suffix = f" fields={', '.join(field_names)}" if field_names else ""
                lines.append(
                    f"- #{tag_name} ({base_type},{suffix}): {_clip_text(instructions, 420)}"
                )

        if qa_entries:
            lines.append("### Accepted Project Q&A")
            for entry in qa_entries:
                question = _clip_text((entry.question or "").strip(), 180)
                answer = _clip_text((entry.answer or "").strip(), 220)
                meta = f"status={entry.status}, review={entry.review_state}"
                line = f"- Q: {question} ({meta}, asked={entry.asked_count})"
                if answer:
                    line += f" / A: {answer}"
                lines.append(line)

        if edges:
            lines.append("### Docs References")
            for edge in edges[:24]:
                source = node_title_by_id.get(edge.source_node_id, str(edge.source_node_id))
                target = node_title_by_id.get(edge.target_node_id, str(edge.target_node_id))
                lines.append(
                    f"- {source} -> {target} ({edge.relation_type}, confidence={edge.confidence})"
                )

        if revisions:
            lines.append("### Canonical Revision Meta")
            for revision in revisions:
                refs = revision.source_refs_json or []
                ref_note = f", source_refs={len(refs)}" if refs else ""
                lines.append(
                    f"- {revision.created_at.isoformat() if revision.created_at else ''}: "
                    f"{_clip_text(revision.change_summary or '', 180)}{ref_note}"
                )

        if tables:
            lines.append("### Record Tables")
            for table in tables:
                description = _clip_text((table.description or "").strip(), 140)
                line = f"- {table.name}.dbtable"
                if description:
                    line += f": {description}"
                lines.append(line)

        return "\n".join(lines)
