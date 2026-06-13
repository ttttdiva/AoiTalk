"""Project context pack storage and prompt rendering."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select

from ..memory.database import get_db_session
from ..memory.models import ProjectContextPack


def _coerce_uuid(value: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _append_json_or_text(lines: list[str], value: Any, prefix: str = "  ") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lines.append(f"{prefix}- {key}: {item}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                lines.append(f"{prefix}- {json.dumps(item, ensure_ascii=False)}")
            else:
                lines.append(f"{prefix}- {item}")
    elif value not in (None, ""):
        lines.append(f"{prefix}{value}")


class ProjectContextPackService:
    """CRUD helpers for a project's short canonical context."""

    _updatable_fields = {
        "summary_md",
        "goals",
        "constraints",
        "current_status",
        "active_task_snapshot",
        "decisions",
        "open_questions",
        "manual_notes",
        "generated_from",
    }

    async def get_project_context_pack(
        self, project_id: str
    ) -> Optional[Dict[str, Any]]:
        async with await get_db_session() as session:
            result = await session.execute(
                select(ProjectContextPack).where(
                    ProjectContextPack.project_id == _coerce_uuid(project_id)
                )
            )
            pack = result.scalar_one_or_none()
            return pack.to_dict() if pack else None

    async def upsert_project_context_pack(
        self, project_id: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        project_uuid = _coerce_uuid(project_id)
        async with await get_db_session() as session:
            result = await session.execute(
                select(ProjectContextPack).where(
                    ProjectContextPack.project_id == project_uuid
                )
            )
            pack = result.scalar_one_or_none()
            if pack is None:
                pack = ProjectContextPack(id=uuid.uuid4(), project_id=project_uuid)
                session.add(pack)

            for field in self._updatable_fields:
                if field not in data:
                    continue
                value = data[field]
                if field in {
                    "goals",
                    "constraints",
                    "active_task_snapshot",
                    "decisions",
                    "open_questions",
                }:
                    value = _as_list(value)
                elif field in {"current_status", "generated_from"}:
                    value = _as_dict(value)
                elif value is None:
                    value = ""
                setattr(pack, field, value)
            pack.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(pack)
            return pack.to_dict()

    async def render_project_context_pack_for_prompt(self, project_id: str) -> str:
        pack = await self.get_project_context_pack(project_id)
        return self.render_pack_dict(pack)

    @staticmethod
    def render_pack_dict(pack: Optional[Dict[str, Any]]) -> str:
        if not pack:
            return ""

        lines = ["## Project Context Pack"]
        summary = (pack.get("summary_md") or "").strip()
        if summary:
            lines.append(f"- Summary: {summary}")

        sections = [
            ("Goals", pack.get("goals")),
            ("Constraints", pack.get("constraints")),
            ("Current Status", pack.get("current_status")),
            ("Active Tasks", pack.get("active_task_snapshot")),
            ("Decisions", pack.get("decisions")),
            ("Open Questions", pack.get("open_questions")),
        ]
        for title, value in sections:
            if value in (None, "", [], {}):
                continue
            lines.append(f"- {title}:")
            _append_json_or_text(lines, value)

        manual_notes = (pack.get("manual_notes") or "").strip()
        if manual_notes:
            lines.append("- Manual Notes:")
            lines.append(f"  {manual_notes}")

        return "\n".join(lines) if len(lines) > 1 else ""


_service = ProjectContextPackService()


async def get_project_context_pack(project_id: str) -> Optional[Dict[str, Any]]:
    return await _service.get_project_context_pack(project_id)


async def upsert_project_context_pack(project_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    return await _service.upsert_project_context_pack(project_id, data)


async def render_project_context_pack_for_prompt(project_id: str) -> str:
    return await _service.render_project_context_pack_for_prompt(project_id)
