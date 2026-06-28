"""Build compact runtime context blocks for LLM prompts."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from sqlalchemy import select

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


@dataclass
class ContextBundle:
    memory_context_block: str = ""
    project_context_block: str = ""
    project_information_block: str = ""
    project_pack_block: str = ""
    task_context_block: str = ""
    session_context_block: str = ""
    debug: Dict[str, Any] = field(default_factory=dict)
    max_chars: int = 12000

    def render_for_prompt(self, max_chars: Optional[int] = None) -> str:
        limit = max_chars or self.max_chars
        blocks = [
            self.project_context_block,
            self.project_information_block,
            self.memory_context_block,
            self.project_pack_block,
            self.task_context_block,
            self.session_context_block,
        ]
        seen: set[str] = set()
        rendered: list[str] = []
        total = 0
        for block in blocks:
            block = (block or "").strip()
            if not block or block in seen:
                continue
            seen.add(block)
            next_total = total + len(block) + (2 if rendered else 0)
            if next_total > limit:
                remaining = limit - total - (2 if rendered else 0)
                if remaining > 80:
                    rendered.append(_clip_text(block, remaining))
                break
            rendered.append(block)
            total = next_total
        return "\n\n".join(rendered)


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

    async def _build_project_information_block(
        self,
        *,
        project_id: str,
        max_facts: int = 8,
        max_documents: int = 6,
        max_record_tables: int = 6,
        fact_content_chars: int = 220,
        document_description_chars: int = 160,
    ) -> str:
        project_uuid = _coerce_uuid(project_id)
        if not project_uuid:
            return ""

        from ..memory.models import ProjectDocument, ProjectFact, RecordTable

        async with await get_db_session() as session:
            facts_result = await session.execute(
                select(ProjectFact)
                .where(
                    ProjectFact.project_id == project_uuid,
                    ProjectFact.deleted_at.is_(None),
                    ProjectFact.status != "archived",
                )
                .order_by(ProjectFact.importance.desc(), ProjectFact.updated_at.desc())
                .limit(max(1, max_facts))
            )
            documents_result = await session.execute(
                select(ProjectDocument)
                .where(
                    ProjectDocument.project_id == project_uuid,
                    ProjectDocument.deleted_at.is_(None),
                    ProjectDocument.status != "archived",
                )
                .order_by(ProjectDocument.is_primary.desc(), ProjectDocument.updated_at.desc())
                .limit(max(1, max_documents))
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

            facts = list(facts_result.scalars().all())
            documents = list(documents_result.scalars().all())
            tables = list(tables_result.scalars().all())

        if not facts and not documents and not tables:
            return ""

        lines = [
            "## Project Information DB Snapshot",
            (
                "Saved project facts/documents/tables only. Use this as grounded "
                "case context and do not infer missing details."
            ),
        ]
        if facts:
            lines.append("- Facts:")
            for fact in facts:
                title = (fact.title or "(untitled)").strip()
                content = _clip_text((fact.content or "").strip(), fact_content_chars)
                meta = f"type={fact.fact_type}, importance={fact.importance}"
                if fact.confidence is not None:
                    meta += f", confidence={fact.confidence:g}"
                lines.append(f"  - {title}: {content} ({meta})")

        if documents:
            lines.append("- Documents:")
            for document in documents:
                title = (document.title or "(untitled)").strip()
                target = (
                    document.file_path
                    or document.external_url
                    or (
                        f"record_table:{document.record_table_id}"
                        if document.record_table_id
                        else ""
                    )
                    or "metadata_only"
                )
                description = _clip_text(
                    (document.description or "").strip(),
                    document_description_chars,
                )
                detail = (
                    f"{document.document_type}, {document.role}, "
                    f"target={target}"
                )
                if document.is_primary:
                    detail = f"primary, {detail}"
                line = f"  - {title} ({detail})"
                if description:
                    line += f": {description}"
                lines.append(line)

        if tables:
            lines.append("- Record Tables:")
            for table in tables:
                description = _clip_text((table.description or "").strip(), 140)
                line = f"  - {table.name}.dbtable"
                if description:
                    line += f": {description}"
                lines.append(line)

        return "\n".join(lines)
