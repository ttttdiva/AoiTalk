"""Project metadata normalization and runtime project context helpers."""

from __future__ import annotations

import re
from pathlib import Path
from contextvars import ContextVar, Token
from copy import deepcopy
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_PROJECT_METADATA_SCHEMA_VERSION = 1
DEFAULT_TASK_RULES = {
    "auto_create_followup": True,
    "auto_create_due_task": False,
    "require_confirmation_for_wbs_change": True,
}

_KNOWN_FLAT_LINK_KEYS = {
    "workspace_root",
}
_KNOWN_FLAT_MANAGEMENT_KEYS = {
    "wbs_file",
    "status_file",
    "issue_file",
    "risk_file",
    "request_files",
    "task_rules",
}
_KNOWN_TOP_LEVEL_KEYS = {"schema_version", "links", "management", "workspace_tools_enabled"} | _KNOWN_FLAT_LINK_KEYS | _KNOWN_FLAT_MANAGEMENT_KEYS

_runtime_project_context: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "runtime_project_context",
    default=None,
)


def _clean_optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _normalize_path(value: Any) -> Optional[str]:
    text = _clean_optional_string(value)
    if not text:
        return None
    return text.replace("\\", "/").rstrip("/")


def _normalize_project_file_path(value: Any) -> Optional[str]:
    text = _normalize_path(value)
    if not text:
        return None
    if re.match(r"^[A-Za-z]:/", text) or text.startswith("/") or text.startswith("//"):
        return None
    match = re.match(r"^_projects/project_[^/]+/(.*)$", text, flags=re.IGNORECASE)
    if match:
        text = match.group(1)
    normalized = str(Path(text).as_posix()).strip("/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _normalize_project_file_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        normalized = _normalize_project_file_path(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _project_storage_path(project_id: Any) -> Optional[str]:
    text = _clean_optional_string(project_id)
    return f"_projects/project_{text}" if text else None


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def normalize_project_metadata(metadata: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Normalize free-form project metadata into a stable schema."""
    raw = deepcopy(metadata or {})
    links_input = raw.get("links") if isinstance(raw.get("links"), dict) else {}
    management_input = raw.get("management") if isinstance(raw.get("management"), dict) else {}

    links = {
        "workspace_root": _normalize_path(
            links_input.get("workspace_root", raw.get("workspace_root"))
        ),
    }

    task_rules_input = management_input.get("task_rules")
    if not isinstance(task_rules_input, dict):
        task_rules_input = raw.get("task_rules") if isinstance(raw.get("task_rules"), dict) else {}

    task_rules = {
        "auto_create_followup": _coerce_bool(
            task_rules_input.get("auto_create_followup"),
            DEFAULT_TASK_RULES["auto_create_followup"],
        ),
        "auto_create_due_task": _coerce_bool(
            task_rules_input.get("auto_create_due_task"),
            DEFAULT_TASK_RULES["auto_create_due_task"],
        ),
        "require_confirmation_for_wbs_change": _coerce_bool(
            task_rules_input.get("require_confirmation_for_wbs_change"),
            DEFAULT_TASK_RULES["require_confirmation_for_wbs_change"],
        ),
    }

    management = {
        "wbs_file": _normalize_project_file_path(
            management_input.get("wbs_file", raw.get("wbs_file"))
        ),
        "issue_file": _normalize_project_file_path(
            management_input.get("issue_file", raw.get("issue_file"))
        ),
        "risk_file": _normalize_project_file_path(
            management_input.get("risk_file", raw.get("risk_file"))
        ),
        "request_files": _normalize_project_file_list(
            management_input.get("request_files", raw.get("request_files"))
        ),
        "task_rules": task_rules,
    }

    normalized = {
        key: value
        for key, value in raw.items()
        if key not in _KNOWN_TOP_LEVEL_KEYS
    }
    normalized["schema_version"] = DEFAULT_PROJECT_METADATA_SCHEMA_VERSION
    normalized["links"] = links
    normalized["management"] = management
    normalized["workspace_tools_enabled"] = _coerce_bool(raw.get("workspace_tools_enabled"), False)
    return normalized


def merge_project_metadata(
    existing: Optional[dict[str, Any]],
    updates: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Merge a partial metadata update into existing metadata."""
    normalized_existing = normalize_project_metadata(existing)
    if not updates:
        return normalized_existing
    merged = _deep_merge(normalized_existing, deepcopy(updates))
    return normalize_project_metadata(merged)


def get_runtime_project_context() -> Optional[dict[str, Any]]:
    """Return the project context for the current request, if any."""
    return _runtime_project_context.get()


def set_runtime_project_context(project_context: Optional[dict[str, Any]]) -> Token:
    """Set request-local project context and return the reset token."""
    return _runtime_project_context.set(project_context)


def reset_runtime_project_context(token: Token) -> None:
    """Reset request-local project context to its previous value."""
    _runtime_project_context.reset(token)


def _normalize_path_for_match(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").casefold()


def _path_matches_workspace_root(workspace_root: Optional[str], workspace_path: Optional[str]) -> bool:
    if not workspace_root or not workspace_path:
        return False
    normalized_root = _normalize_path_for_match(workspace_root)
    normalized_path = _normalize_path_for_match(workspace_path)
    return normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/")


def build_project_context(project: Any) -> Optional[dict[str, Any]]:
    """Build a prompt/runtime-friendly project context from a project model or dict."""
    if not project:
        return None

    if isinstance(project, dict):
        metadata = project.get("metadata", project.get("project_metadata"))
        context = {
            "id": project.get("id"),
            "name": project.get("name"),
            "slug": project.get("slug"),
            "description": project.get("description"),
        }
    else:
        metadata = getattr(project, "project_metadata", None)
        context = {
            "id": str(getattr(project, "id", "")) or None,
            "name": getattr(project, "name", None),
            "slug": getattr(project, "slug", None),
            "description": getattr(project, "description", None),
        }

    normalized_metadata = normalize_project_metadata(metadata)
    context["metadata"] = normalized_metadata
    context["workspace_root"] = normalized_metadata["links"]["workspace_root"]
    context["project_storage_path"] = _project_storage_path(context.get("id"))
    context["wbs_file"] = normalized_metadata["management"]["wbs_file"]
    context["issue_file"] = normalized_metadata["management"].get("issue_file")
    context["risk_file"] = normalized_metadata["management"].get("risk_file")
    context["request_files"] = normalized_metadata["management"].get("request_files", [])
    context["task_rules"] = normalized_metadata["management"]["task_rules"]
    context["workspace_tools_enabled"] = normalized_metadata["workspace_tools_enabled"]
    return context


def format_project_context_for_prompt(project_context: Optional[dict[str, Any]]) -> str:
    """Render the full project-management prompt block for the active project context."""
    if not project_context:
        return ""

    metadata = project_context.get("metadata", {})
    management = metadata.get("management", {})
    task_rules = management.get("task_rules", {})

    lines = [
        "Active project context:",
        f"- project_id: {project_context.get('id') or 'unknown'}",
        f"- project_name: {project_context.get('name') or 'unknown'}",
        f"- project_slug: {project_context.get('slug') or 'unknown'}",
        f"- project_files: {project_context.get('project_storage_path') or 'not_set'}",
        f"- wbs_file: {management.get('wbs_file') or 'not_set'}",
        f"- issue_file: {management.get('issue_file') or 'not_set'}",
        f"- risk_file: {management.get('risk_file') or 'not_set'}",
        (
            "- task_rules: "
            f"auto_followup={task_rules.get('auto_create_followup')}, "
            f"auto_due={task_rules.get('auto_create_due_task')}, "
            f"confirm_wbs={task_rules.get('require_confirmation_for_wbs_change')}"
        ),
    ]
    return "\n".join(lines)


def sanitize_project_context_for_chat(
    project_context: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return only project identity fields that are safe for ordinary chat prompts."""
    if not project_context:
        return None

    sanitized = {
        key: project_context.get(key)
        for key in ("id", "name", "slug", "description", "match_reason")
        if project_context.get(key)
    }
    record_tables = project_context.get("record_tables")
    if isinstance(record_tables, list) and record_tables:
        sanitized["record_tables"] = record_tables[:20]
    return sanitized or None


def format_project_context_for_chat_prompt(project_context: Optional[dict[str, Any]]) -> str:
    """Render selected project identity without surfacing optional management settings."""
    sanitized = sanitize_project_context_for_chat(project_context)
    if not sanitized:
        return ""

    lines = ["Selected project context:"]
    if sanitized.get("id"):
        lines.append(f"- project_id: {sanitized['id']}")
    if sanitized.get("name"):
        lines.append(f"- project_name: {sanitized['name']}")
    if sanitized.get("slug"):
        lines.append(f"- project_slug: {sanitized['slug']}")
    if sanitized.get("description"):
        lines.append(f"- project_description: {sanitized['description']}")
    if sanitized.get("record_tables"):
        names = [
            str(item.get("name"))
            for item in sanitized["record_tables"]
            if isinstance(item, dict) and item.get("name")
        ]
        if names:
            lines.append(f"- available_tables: {', '.join(names[:20])}")
    lines.append(
        "- This identifies the selected project only. Do not volunteer missing optional "
        "project metadata unless the user asks to inspect or configure it."
    )
    return "\n".join(lines)


def format_minimal_project_context_for_chat_prompt(
    project_context: Optional[dict[str, Any]],
) -> str:
    """Render only selected project identity for lightweight CLI prompts."""
    if not project_context:
        return ""

    lines = ["選択中プロジェクト:"]
    if project_context.get("id"):
        lines.append(f"- project_id: {project_context['id']}")
    if project_context.get("name"):
        lines.append(f"- project_name: {project_context['name']}")
    if project_context.get("slug"):
        lines.append(f"- project_slug: {project_context['slug']}")
    return "\n".join(lines) if len(lines) > 1 else ""


class ProjectContextResolver:
    """Resolve project context from project, session, or workspace inputs."""

    def __init__(self, db_manager: Any = None):
        self.db_manager = db_manager

    async def _get_session(self) -> AsyncSession:
        if self.db_manager is not None:
            return await self.db_manager.get_session()
        from ..memory.database import get_database_manager

        db_manager = get_database_manager()
        return await db_manager.get_session()

    async def get_project_context(
        self,
        project_id: str,
        *,
        user_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        from ..memory.project_repository import ProjectRepository

        session = await self._get_session()
        try:
            project_uuid = UUID(project_id)
            project = await ProjectRepository.get_by_id(session, project_uuid)
            if not project:
                return None

            if user_id:
                try:
                    member = await ProjectRepository.get_member(
                        session,
                        project_uuid,
                        UUID(user_id),
                    )
                except (ValueError, TypeError):
                    member = None
                if member is None:
                    return None

            context = build_project_context(project)
            if context:
                context["match_reason"] = "project_id"
                try:
                    from ..memory.models import RecordTable

                    result = await session.execute(
                        select(RecordTable)
                        .where(
                            RecordTable.project_id == project_uuid,
                            RecordTable.deleted_at.is_(None),
                        )
                        .order_by(RecordTable.sort_order, RecordTable.created_at)
                        .limit(20)
                    )
                    context["record_tables"] = [
                        {
                            "id": str(table.id),
                            "name": table.name,
                            "description": table.description,
                        }
                        for table in result.scalars().all()
                    ]
                except Exception:
                    context["record_tables"] = []
            return context
        finally:
            await session.close()

    async def resolve_context(
        self,
        *,
        project_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if project_id:
            return await self.get_project_context(project_id, user_id=user_id)

        resolved_project_id = await self._resolve_project_id_from_session(session_id)
        if resolved_project_id:
            context = await self.get_project_context(resolved_project_id, user_id=user_id)
            if context:
                context["match_reason"] = "session_id"
            return context

        if workspace_path:
            context = await self._resolve_by_workspace_path(workspace_path, user_id=user_id)
            if context:
                return context

        return None

    async def _resolve_project_id_from_session(self, session_id: Optional[str]) -> Optional[str]:
        if not session_id:
            return None

        from ..memory.models import ConversationSession

        session = await self._get_session()
        try:
            result = await session.execute(
                select(ConversationSession).where(ConversationSession.id == UUID(session_id))
            )
            conversation_session = result.scalar_one_or_none()
            if not conversation_session or not conversation_session.project_id:
                return None
            return str(conversation_session.project_id)
        except (ValueError, TypeError):
            return None
        finally:
            await session.close()

    async def _get_candidate_projects(
        self,
        session: AsyncSession,
        *,
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        from ..memory.models import Project
        from ..memory.project_repository import ProjectRepository

        if user_id:
            try:
                return await ProjectRepository.get_user_projects(session, UUID(user_id))
            except (ValueError, TypeError):
                return []

        result = await session.execute(select(Project))
        return [project.to_dict() for project in result.scalars().all()]

    async def _resolve_by_workspace_path(
        self,
        workspace_path: str,
        *,
        user_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        session = await self._get_session()
        try:
            candidates = await self._get_candidate_projects(session, user_id=user_id)
            best_match: Optional[dict[str, Any]] = None
            best_length = -1

            for project in candidates:
                context = build_project_context(project)
                workspace_root = context.get("workspace_root") if context else None
                project_storage_path = context.get("project_storage_path") if context else None
                workspace_match = _path_matches_workspace_root(workspace_root, workspace_path)
                project_storage_match = _path_matches_workspace_root(
                    project_storage_path,
                    workspace_path,
                )
                if not workspace_match and not project_storage_match:
                    continue

                matched_root = project_storage_path if project_storage_match else workspace_root
                normalized_length = len(_normalize_path_for_match(matched_root))
                if normalized_length > best_length:
                    best_match = context
                    best_length = normalized_length

            if best_match:
                best_match["match_reason"] = "workspace_path"
            return best_match
        finally:
            await session.close()
