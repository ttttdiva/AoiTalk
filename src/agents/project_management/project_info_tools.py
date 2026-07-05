"""Docs正本の案件情報関連ツール。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from ...tools.core import tool
from .common import (
    _clean_text,
    _json,
    _management_documents_from_project,
    _nullable_text,
    _one_of,
    _parse_datetime,
    _parse_optional_uuid,
    _project_reference_match_score,
    _resolve_actor_and_project,
    _resolve_operator_user_id,
    _resolve_project,
    _run_async,
)


project_qa_statuses = {"unanswered", "answered", "stale", "archived"}
project_qa_review_states = {"candidate", "accepted", "rejected"}


def _normalize_management_file_path(
    project_id: UUID,
    storage_root: Path,
    value: str,
) -> str:
    text = str(value or "").strip().replace("\\", "/")
    prefix = f"_projects/project_{project_id}/"
    if text.lower().startswith(prefix.lower()):
        text = text[len(prefix) :]
    text = text.lstrip("/")
    if not text or Path(text).is_absolute():
        raise ValueError("Management file path must be relative to the project workspace.")

    target = (storage_root / Path(text)).resolve()
    try:
        target.relative_to(storage_root.resolve())
    except ValueError as exc:
        raise ValueError("Management file path escapes the project workspace.") from exc
    if not target.is_file():
        raise FileNotFoundError(f"Management file was not found: {text}")
    return text


def _clip_project_info_text(value: object, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "..."


def _bounded_project_info_limit(value: object, default: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 50))


def _normalized_project_question_hash(value: str) -> str:
    normalized = " ".join(str(value or "").strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _json_array(value: str) -> list:
    if not str(value or "").strip():
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("JSON value must be an array.")
    return parsed


def _compact_project_information_payload(
    payload: dict,
    *,
    docs_node_chars: int,
    record_table_limit: int,
) -> dict:
    project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
    node = payload.get("node") if isinstance(payload.get("node"), dict) else {}
    record_tables = (
        payload.get("record_tables")
        if isinstance(payload.get("record_tables"), list)
        else []
    )
    qa_entries = (
        payload.get("qa_entries")
        if isinstance(payload.get("qa_entries"), list)
        else []
    )
    return {
        "project": {
            "id": project.get("id"),
            "name": project.get("name"),
            "slug": project.get("slug"),
            "description": _clip_project_info_text(project.get("description"), 240),
            "knowledge_node_id": project.get("knowledge_node_id"),
        },
        "counts": {
            "qa_entries": len(qa_entries),
            "record_tables": len(record_tables),
            "management_documents": len(payload.get("management_documents") or []),
        },
        "node": {
            "id": node.get("id"),
            "title": node.get("title"),
            "body_text": _clip_project_info_text(
                node.get("body_text"),
                docs_node_chars,
            ),
            "updated_at": node.get("updated_at"),
        },
        "qa_entries": [
            {
                "id": item.get("id"),
                "question": _clip_project_info_text(item.get("question"), 180),
                "answer": _clip_project_info_text(item.get("answer"), 220),
                "status": item.get("status"),
                "review_state": item.get("review_state"),
                "asked_count": item.get("asked_count"),
            }
            for item in qa_entries[:8]
            if isinstance(item, dict)
        ],
        "record_tables": record_tables[:record_table_limit],
        "management_documents": payload.get("management_documents") or [],
        "output_note": (
            "案件情報の正本はDocs nodeです。"
            "必要な場合だけ detail_level='full' で全文を取得してください。"
        ),
    }


def build_project_info_tools() -> list:
    """Docs正本の案件情報ツール群を生成して返す。"""

    @tool
    def get_project_context(project_id: str = "") -> str:
        """Get the active project context or a specific project context/name."""
        from ...memory.database import get_database_manager
        from ...services.project_context import (
            ProjectContextResolver,
            format_project_context_for_prompt,
            get_runtime_project_context,
        )

        if not project_id:
            context = get_runtime_project_context()
            return (
                format_project_context_for_prompt(context)
                if context
                else "No active project context."
            )

        async def _resolve_context():
            db_manager = get_database_manager()
            session = await db_manager.get_session()
            try:
                project = await _resolve_project(session, project_id)
                if not project:
                    return None
            finally:
                await session.close()

            resolver = ProjectContextResolver(db_manager=db_manager)
            return await resolver.get_project_context(str(project.id))

        context = _run_async(_resolve_context())
        if not context:
            return f"Project not found: {project_id}"
        return format_project_context_for_prompt(context)

    @tool
    def list_projects(project: str = "") -> str:
        """List accessible projects and optionally filter by UUID, slug, or project name."""
        from ...memory.database import get_database_manager
        from ...memory.models import Project
        from ...memory.project_repository import ProjectRepository

        async def _list():
            db = get_database_manager()
            session = await db.get_session()
            try:
                operator_user_id = await _resolve_operator_user_id(session)
                projects = await ProjectRepository.get_user_projects(
                    session,
                    user_id=operator_user_id,
                )
                project_filter = (project or "").strip()
                if not project_filter:
                    return projects
                matches = [
                    item
                    for item in projects
                    if _project_reference_match_score(item, project_filter) > 0
                ]
                if matches:
                    return matches

                fallback_result = await session.execute(
                    select(Project).where(Project.deleted_at.is_(None)).limit(200)
                )
                return [
                    item.to_dict()
                    for item in fallback_result.scalars().all()
                    if _project_reference_match_score(item.to_dict(), project_filter)
                    > 0
                ]
            finally:
                await session.close()

        return _json(_run_async(_list()))

    @tool
    def list_project_information(
        project: str = "",
        project_id: str = "",
        include_archived: bool = False,
        detail_level: str = "summary",
        limit_record_tables: int = 8,
        docs_node_chars: int = 2000,
    ) -> str:
        """Read the canonical project information Docs source of truth before answering or editing project facts. Prefer detail_level='full' before a write. The response includes Docs body, accepted/candidate Q&A, record tables, and references; use it as grounded evidence and do not invent missing facts."""
        from ...memory.database import get_database_manager
        from ...memory.models import Project, ProjectQaEntry, RecordTable
        from ...services.project_information_docs import (
            ensure_project_information_doc,
            serialize_project_information_node,
        )

        async def _list():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if resolved_project_id is None:
                    raise ValueError("No project could be resolved.")
                project_obj = await session.get(Project, resolved_project_id)
                if project_obj is None:
                    raise ValueError("Project not found.")

                node = await ensure_project_information_doc(
                    session,
                    project=project_obj,
                    user_id=user_id,
                )

                record_tables_result = await session.execute(
                    select(RecordTable)
                    .where(
                        RecordTable.project_id == resolved_project_id,
                        RecordTable.deleted_at.is_(None),
                    )
                    .order_by(RecordTable.sort_order, RecordTable.created_at)
                )
                qa_stmt = select(ProjectQaEntry).where(
                    ProjectQaEntry.project_id == resolved_project_id,
                    ProjectQaEntry.deleted_at.is_(None),
                )
                if not include_archived:
                    qa_stmt = qa_stmt.where(ProjectQaEntry.status != "archived")
                qa_entries_result = await session.execute(
                    qa_stmt.order_by(ProjectQaEntry.updated_at.desc())
                )
                await session.commit()
                payload = {
                    "project": project_obj.to_dict(),
                    "node": serialize_project_information_node(node),
                    "management_documents": _management_documents_from_project(project_obj),
                    "record_tables": [
                        {
                            "id": str(table.id),
                            "name": table.name,
                            "description": table.description,
                            "filer_name": f"{table.name}.dbtable",
                            "updated_at": (
                                table.updated_at.isoformat()
                                if table.updated_at
                                else None
                            ),
                        }
                        for table in record_tables_result.scalars().all()
                    ],
                    "qa_entries": [
                        entry.to_dict()
                        for entry in qa_entries_result.scalars().all()
                    ],
                }
                if str(detail_level or "summary").strip().casefold() in {
                    "full",
                    "all",
                    "detail",
                    "detailed",
                }:
                    return payload
                return _compact_project_information_payload(
                    payload,
                    docs_node_chars=_bounded_project_info_limit(
                        docs_node_chars,
                        2000,
                    )
                    * 100,
                    record_table_limit=_bounded_project_info_limit(
                        limit_record_tables,
                        8,
                    ),
                )
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_list()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def patch_project_information_doc(
        project: str = "",
        project_id: str = "",
        title: str = "",
        body_text: str = "",
        append_text: str = "",
        section_heading: str = "",
        operation: str = "append",
        change_summary: str = "",
        source_refs_json: str = "",
        update_reason: str = "案件情報Docs正本を更新",
    ) -> str:
        """Patch the canonical project information Docs source of truth. Respect existing headings; use section_heading with operation='append' or 'replace' for section/block edits, and only use body_text for deliberate full-document replacement. Always provide change_summary and source_refs_json (JSON array) when evidence exists; unsupported claims belong in a 要確認 section or a candidate Q&A entry."""
        from ...memory.database import get_database_manager
        from ...memory.models import Project
        from ...services.project_information_docs import (
            serialize_project_information_node,
            update_project_information_doc,
        )

        async def _patch():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if resolved_project_id is None:
                    raise ValueError("No project could be resolved.")
                project_obj = await session.get(Project, resolved_project_id)
                if project_obj is None:
                    raise ValueError("Project not found.")
                if not body_text.strip() and not append_text.strip() and not title.strip():
                    raise ValueError("body_text, append_text, or title is required.")
                source_refs = _json_array(source_refs_json) if source_refs_json.strip() else []
                summary = _clean_text(
                    change_summary or update_reason,
                    "案件情報Docs正本を更新",
                )
                node = await update_project_information_doc(
                    session,
                    project=project_obj,
                    user_id=user_id,
                    title=title if title.strip() else None,
                    body_text=body_text if body_text.strip() else None,
                    append_text=append_text if append_text.strip() else None,
                    section_heading=section_heading if section_heading.strip() else None,
                    operation=_one_of(operation, {"append", "replace"}, "append"),
                    change_summary=summary,
                    source_refs=source_refs,
                )
                await session.commit()
                return {
                    "success": True,
                    "node": serialize_project_information_node(node),
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_patch()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def attach_project_information_reference(
        title: str,
        project: str = "",
        project_id: str = "",
        file_path: str = "",
        external_url: str = "",
        record_table_id: str = "",
        description: str = "",
    ) -> str:
        """Append a reference link to the canonical project information Docs node."""
        target = file_path.strip() or external_url.strip() or record_table_id.strip()
        if not title.strip() or not target:
            return _json({"success": False, "error": "title and a reference target are required."})
        append_text = "\n".join(
            [
                "## 参照追加",
                "",
                f"- **{title.strip()}**: `{target}`"
                + (f" - {description.strip()}" if description.strip() else ""),
                "",
            ]
        )
        return patch_project_information_doc(
            project=project,
            project_id=project_id,
            append_text=append_text,
            update_reason="案件情報Docs正本へ参照を追加",
        )

    @tool
    def organize_project_information_from_folder(
        folder_path: str = "",
        project: str = "",
        project_id: str = "",
        apply: bool = False,
        use_llm: bool = True,
        max_files: int = 80,
    ) -> str:
        """Scan a project filer folder and organize the result into the canonical Docs node."""
        from ...config import Config
        from ...memory.database import get_database_manager
        from ...memory.models import Project
        from ...memory.project_repository import ProjectRepository
        from ...services.project_information_organizer import organize_project_folder
        from ...tools.file_explorer import get_root_dir as get_workspace_root

        async def _organize():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if resolved_project_id is None:
                    raise ValueError("No project could be resolved.")

                project_obj = await session.get(Project, resolved_project_id)
                if project_obj is None:
                    raise ValueError("Project not found.")

                storage_root = await ProjectRepository.get_storage_path(
                    resolved_project_id
                )
                storage_root_path = (get_workspace_root() / storage_root).resolve()
                storage_root_path.mkdir(parents=True, exist_ok=True)

                config = None
                if use_llm:
                    try:
                        config = Config()
                        config.set("use_tools", False)
                    except Exception:
                        config = None

                return await organize_project_folder(
                    session,
                    project_id=resolved_project_id,
                    project_name=project_obj.name,
                    user_id=user_id,
                    storage_root=storage_root_path,
                    folder_path=folder_path,
                    apply=apply,
                    use_llm=use_llm,
                    config=config,
                    max_files=max(1, min(200, int(max_files or 80))),
                )
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_organize()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def configure_project_management_files(
        project: str = "",
        project_id: str = "",
        wbs_file: str = "",
        issue_file: str = "",
        risk_file: str = "",
        request_files_json: str = "",
    ) -> str:
        """Set project WBS, issue, risk, and request files using paths relative to the project workspace."""
        from ...memory.database import get_database_manager
        from ...memory.models import Project
        from ...memory.project_repository import ProjectRepository
        from ...tools.file_explorer import get_root_dir as get_workspace_root

        async def _configure():
            db = get_database_manager()
            session = await db.get_session()
            try:
                _, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if resolved_project_id is None:
                    raise ValueError("No project could be resolved.")
                project_obj = await session.get(Project, resolved_project_id)
                if project_obj is None:
                    raise ValueError("Project not found.")

                storage_path = await ProjectRepository.get_storage_path(
                    resolved_project_id
                )
                storage_root = (get_workspace_root() / storage_path).resolve()
                storage_root.mkdir(parents=True, exist_ok=True)

                metadata = dict(project_obj.project_metadata or {})
                management = dict(metadata.get("management") or {})
                updates = {
                    "wbs_file": wbs_file,
                    "issue_file": issue_file,
                    "risk_file": risk_file,
                }
                for key, value in updates.items():
                    if str(value or "").strip():
                        management[key] = _normalize_management_file_path(
                            resolved_project_id,
                            storage_root,
                            value,
                        )

                if request_files_json.strip():
                    decoded = json.loads(request_files_json)
                    if not isinstance(decoded, list):
                        raise ValueError("request_files_json must be a JSON array.")
                    management["request_files"] = [
                        _normalize_management_file_path(
                            resolved_project_id,
                            storage_root,
                            str(value),
                        )
                        for value in decoded
                    ]

                metadata["management"] = management
                project_obj.project_metadata = metadata
                project_obj.updated_at = datetime.utcnow()
                await session.commit()
                return {
                    "success": True,
                    "project_id": str(resolved_project_id),
                    "project_name": project_obj.name,
                    "management": management,
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_configure()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def upsert_project_qa_entry(
        question: str,
        answer: str = "",
        project: str = "",
        project_id: str = "",
        qa_entry_id: str = "",
        status: str = "",
        review_state: str = "accepted",
        confidence: float = 1.0,
        source_session_id: str = "",
        source_message_ids_json: str = "",
        source_agent_run_ids_json: str = "",
        source_tool_call_ids_json: str = "",
        answer_source_refs_json: str = "",
    ) -> str:
        """Create or update an accepted Q&A entry linked to the project information Docs node."""
        from ...memory.database import get_database_manager
        from ...memory.models import Project, ProjectQaEntry
        from ...services.project_information_docs import ensure_project_information_doc

        async def _upsert():
            db = get_database_manager()
            session = await db.get_session()
            try:
                user_id, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if resolved_project_id is None:
                    raise ValueError("No project could be resolved.")
                project_obj = await session.get(Project, resolved_project_id)
                if project_obj is None:
                    raise ValueError("Project not found.")
                node = await ensure_project_information_doc(
                    session,
                    project=project_obj,
                    user_id=user_id,
                )
                target_question = _clean_text(question)
                if not target_question:
                    raise ValueError("question is required.")
                target_answer = _nullable_text(answer)
                question_hash = _normalized_project_question_hash(target_question)

                entry = None
                parsed_entry_id = _parse_optional_uuid(qa_entry_id)
                if parsed_entry_id is not None:
                    entry = await session.get(ProjectQaEntry, parsed_entry_id)
                    if entry and entry.project_id != resolved_project_id:
                        raise ValueError("qa_entry_id belongs to another project.")
                if entry is None:
                    existing_result = await session.execute(
                        select(ProjectQaEntry)
                        .where(
                            ProjectQaEntry.project_id == resolved_project_id,
                            ProjectQaEntry.deleted_at.is_(None),
                            ProjectQaEntry.normalized_question_hash == question_hash,
                        )
                        .limit(1)
                    )
                    entry = existing_result.scalar_one_or_none()
                if entry is None:
                    entry = ProjectQaEntry(
                        project_id=resolved_project_id,
                        knowledge_node_id=node.id,
                        created_by=user_id,
                        asked_count=0,
                    )
                    session.add(entry)

                now = datetime.utcnow()
                entry.knowledge_node_id = node.id
                entry.question = target_question
                entry.answer = target_answer
                entry.normalized_question_hash = question_hash
                entry.status = _one_of(
                    status,
                    project_qa_statuses,
                    "answered" if target_answer else "unanswered",
                )
                entry.review_state = _one_of(
                    review_state,
                    project_qa_review_states,
                    "accepted",
                )
                entry.confidence = max(0.0, min(1.0, float(confidence or 1.0)))
                entry.asked_count = max(1, int(entry.asked_count or 0) + 1)
                if source_session_id.strip():
                    entry.source_session_id = _parse_optional_uuid(source_session_id)
                if source_message_ids_json.strip():
                    entry.source_message_ids = _json_array(source_message_ids_json)
                if source_agent_run_ids_json.strip():
                    entry.source_agent_run_ids = _json_array(source_agent_run_ids_json)
                if source_tool_call_ids_json.strip():
                    entry.source_tool_call_ids = _json_array(source_tool_call_ids_json)
                if answer_source_refs_json.strip():
                    entry.answer_source_refs = _json_array(answer_source_refs_json)
                entry.updated_by = user_id
                entry.last_asked_at = now
                entry.updated_at = now
                entry.created_by_agent = True
                await session.commit()
                return {"success": True, "qa_entry": entry.to_dict()}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_upsert()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def archive_project_qa_entry(qa_entry_id: str) -> str:
        """Archive a project Q&A entry without hard-deleting it."""
        from ...memory.database import get_database_manager
        from ...memory.models import ProjectQaEntry

        async def _archive():
            db = get_database_manager()
            session = await db.get_session()
            try:
                entry = await session.get(ProjectQaEntry, UUID(qa_entry_id))
                if entry is None or entry.deleted_at is not None:
                    raise ValueError("Project Q&A entry not found.")
                await _resolve_actor_and_project(
                    session,
                    project_id=str(entry.project_id),
                )
                now = datetime.utcnow()
                entry.status = "archived"
                entry.deleted_at = now
                entry.updated_at = now
                await session.commit()
                return {"success": True, "qa_entry_id": str(entry.id)}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_archive()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @tool
    def list_project_tasks_changed_since(
        project: str = "",
        project_id: str = "",
        changed_after: str = "",
        limit: int = 50,
    ) -> str:
        """List project tasks changed after an explicit timestamp."""
        from ...memory.database import get_database_manager
        from ...memory.models import Task

        async def _list_changed():
            db = get_database_manager()
            session = await db.get_session()
            try:
                _, resolved_project_id = await _resolve_actor_and_project(
                    session,
                    project=project,
                    project_id=project_id,
                )
                if resolved_project_id is None:
                    raise ValueError("No project could be resolved.")
                checkpoint = _parse_datetime(changed_after)
                stmt = select(Task).where(
                    Task.project_id == resolved_project_id,
                    Task.archived_at.is_(None),
                    Task.deleted_at.is_(None),
                )
                if checkpoint is not None:
                    stmt = stmt.where(Task.updated_at > checkpoint)
                stmt = stmt.order_by(Task.updated_at.asc()).limit(
                    max(1, min(int(limit or 50), 200))
                )
                result = await session.execute(stmt)
                tasks = list(result.scalars().all())
                next_checkpoint = checkpoint
                for task in tasks:
                    if task.updated_at and (
                        next_checkpoint is None or task.updated_at > next_checkpoint
                    ):
                        next_checkpoint = task.updated_at
                return {
                    "project_id": str(resolved_project_id),
                    "changed_after": (
                        checkpoint.isoformat() if checkpoint else None
                    ),
                    "recommended_next_checkpoint": (
                        next_checkpoint.isoformat() if next_checkpoint else None
                    ),
                    "tasks": [
                        {
                            "id": str(task.id),
                            "title": task.title,
                            "description": task.description,
                            "status": task.status,
                            "priority": task.priority,
                            "source": task.source,
                            "created_at": (
                                task.created_at.isoformat()
                                if task.created_at
                                else None
                            ),
                            "updated_at": (
                                task.updated_at.isoformat()
                                if task.updated_at
                                else None
                            ),
                            "completed_at": (
                                task.completed_at.isoformat()
                                if task.completed_at
                                else None
                            ),
                            "metadata": task.task_metadata or {},
                        }
                        for task in tasks
                    ],
                }
            finally:
                await session.close()

        try:
            return _json(_run_async(_list_changed()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    return [
        get_project_context,
        list_projects,
        list_project_information,
        patch_project_information_doc,
        attach_project_information_reference,
        organize_project_information_from_folder,
        configure_project_management_files,
        upsert_project_qa_entry,
        archive_project_qa_entry,
        list_project_tasks_changed_since,
    ]
