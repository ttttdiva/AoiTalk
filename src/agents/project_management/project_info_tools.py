"""プロジェクト情報DB（カテゴリ・文書リンク・ファクト・同期状態）関連ツール。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

from agents import function_tool
from sqlalchemy import select

from .common import (
    _run_async,
    _project_reference_match_score,
    _resolve_operator_user_id,
    _resolve_project,
    _resolve_actor_and_project,
    _parse_datetime,
    _json,
    project_info_category_statuses,
    project_info_item_statuses,
    project_info_target_kinds,
    project_info_ai_access_levels,
    _clean_text,
    _nullable_text,
    _one_of,
    _project_info_category_key,
    _parse_optional_uuid,
    _clamp_project_info_importance,
    _clamp_project_info_confidence,
    _resolve_record_table,
    _ensure_project_info_defaults,
    _resolve_project_info_category,
    _management_documents_from_project,
)


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


def build_project_info_tools() -> list:
    """プロジェクト情報DB（カテゴリ・文書リンク・ファクト・同期状態）関連ツールのツール群を生成して返す。"""

    @function_tool
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

    @function_tool
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

    @function_tool
    def list_project_information(
        project: str = "",
        project_id: str = "",
        include_archived: bool = False,
    ) -> str:
        """List project information categories, document links, durable facts, record tables, and sync state."""
        from ...memory.database import get_database_manager
        from ...memory.models import (
            Project,
            ProjectDocument,
            ProjectFact,
            ProjectInfoCategory,
            ProjectInfoSyncState,
            RecordTable,
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
                await _ensure_project_info_defaults(
                    session,
                    resolved_project_id,
                    user_id,
                )
                project_obj = await session.get(Project, resolved_project_id)

                categories_stmt = select(ProjectInfoCategory).where(
                    ProjectInfoCategory.project_id == resolved_project_id
                )
                documents_stmt = select(ProjectDocument).where(
                    ProjectDocument.project_id == resolved_project_id
                )
                facts_stmt = select(ProjectFact).where(
                    ProjectFact.project_id == resolved_project_id
                )
                if not include_archived:
                    categories_stmt = categories_stmt.where(
                        ProjectInfoCategory.status != "archived"
                    )
                    documents_stmt = documents_stmt.where(
                        ProjectDocument.deleted_at.is_(None),
                        ProjectDocument.status != "archived",
                    )
                    facts_stmt = facts_stmt.where(
                        ProjectFact.deleted_at.is_(None),
                        ProjectFact.status != "archived",
                    )

                categories_result = await session.execute(
                    categories_stmt.order_by(
                        ProjectInfoCategory.sort_order,
                        ProjectInfoCategory.created_at,
                    )
                )
                documents_result = await session.execute(
                    documents_stmt.order_by(
                        ProjectDocument.is_primary.desc(),
                        ProjectDocument.updated_at.desc(),
                    )
                )
                facts_result = await session.execute(
                    facts_stmt.order_by(
                        ProjectFact.importance.desc(),
                        ProjectFact.updated_at.desc(),
                    )
                )
                record_tables_result = await session.execute(
                    select(RecordTable)
                    .where(
                        RecordTable.project_id == resolved_project_id,
                        RecordTable.deleted_at.is_(None),
                    )
                    .order_by(RecordTable.sort_order, RecordTable.created_at)
                )
                sync_states_result = await session.execute(
                    select(ProjectInfoSyncState).where(
                        ProjectInfoSyncState.project_id == resolved_project_id
                    )
                )
                await session.commit()
                return {
                    "project": project_obj.to_dict() if project_obj else None,
                    "categories": [
                        category.to_dict()
                        for category in categories_result.scalars().all()
                    ],
                    "documents": [
                        document.to_dict()
                        for document in documents_result.scalars().all()
                    ],
                    "management_documents": (
                        _management_documents_from_project(project_obj)
                        if project_obj
                        else []
                    ),
                    "facts": [fact.to_dict() for fact in facts_result.scalars().all()],
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
                    "sync_states": [
                        state.to_dict()
                        for state in sync_states_result.scalars().all()
                    ],
                }
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_list()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @function_tool
    def organize_project_information_from_folder(
        folder_path: str = "",
        project: str = "",
        project_id: str = "",
        apply: bool = False,
        use_llm: bool = True,
        max_files: int = 80,
    ) -> str:
        """Scan a project filer folder and organize documents/facts into project information. Use apply=false for preview and apply=true to save."""
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
                        from ...config import Config

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

    @function_tool
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

    @function_tool
    def upsert_project_info_category(
        label: str,
        project: str = "",
        project_id: str = "",
        key: str = "",
        description: str = "",
        status: str = "suggested",
    ) -> str:
        """Create or update a project-specific information category/shelf."""
        from ...memory.database import get_database_manager
        from ...memory.models import ProjectInfoCategory

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
                await _ensure_project_info_defaults(
                    session,
                    resolved_project_id,
                    user_id,
                )
                target_label = _clean_text(label)
                if not target_label:
                    raise ValueError("label is required.")
                requested_key = _clean_text(key)
                target_key = _project_info_category_key(
                    requested_key or target_label
                )
                result = await session.execute(
                    select(ProjectInfoCategory).where(
                        ProjectInfoCategory.project_id == resolved_project_id,
                        ProjectInfoCategory.key == target_key,
                    )
                )
                category = result.scalar_one_or_none()
                if category is None:
                    category = await _resolve_project_info_category(
                        session,
                        resolved_project_id,
                        target_label,
                        create_if_missing=True,
                        user_id=user_id,
                        status=status,
                    )
                if requested_key:
                    category.key = target_key
                category.label = target_label[:200]
                if description:
                    category.description = description.strip()
                category.status = _one_of(
                    status,
                    project_info_category_statuses,
                    "suggested",
                )
                category.source = "agent"
                category.updated_at = datetime.utcnow()
                await session.commit()
                return {"success": True, "category": category.to_dict()}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_upsert()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @function_tool
    def register_project_document(
        title: str,
        project: str = "",
        project_id: str = "",
        category: str = "",
        category_id: str = "",
        document_type: str = "document",
        target_kind: str = "file",
        file_path: str = "",
        record_table: str = "",
        record_table_id: str = "",
        external_url: str = "",
        role: str = "reference",
        is_primary: bool = False,
        ai_access_level: str = "metadata",
        description: str = "",
        notes: str = "",
        source_ref: str = "",
    ) -> str:
        """Register or update an important project document link."""
        from ...memory.database import get_database_manager
        from ...memory.models import ProjectDocument

        async def _register():
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
                await _ensure_project_info_defaults(
                    session,
                    resolved_project_id,
                    user_id,
                )
                category_obj = await _resolve_project_info_category(
                    session,
                    resolved_project_id,
                    category,
                    category_id=category_id,
                    create_if_missing=bool(category or category_id),
                    user_id=user_id,
                    status="active",
                )
                normalized_target_kind = _one_of(
                    target_kind,
                    project_info_target_kinds,
                    "file",
                )
                resolved_record_table_id = None
                target_title_input = title
                if normalized_target_kind == "record_table":
                    table_ref = record_table_id or record_table
                    table = await _resolve_record_table(
                        session,
                        resolved_project_id,
                        table_ref,
                    )
                    resolved_record_table_id = table.id
                    if not target_title_input:
                        target_title_input = table.name

                target_title = _clean_text(target_title_input)
                if not target_title:
                    raise ValueError("title is required.")

                stmt = select(ProjectDocument).where(
                    ProjectDocument.project_id == resolved_project_id,
                    ProjectDocument.deleted_at.is_(None),
                )
                if normalized_target_kind == "file" and file_path:
                    stmt = stmt.where(ProjectDocument.file_path == file_path.strip())
                elif normalized_target_kind == "url" and external_url:
                    stmt = stmt.where(
                        ProjectDocument.external_url == external_url.strip()
                    )
                elif normalized_target_kind == "record_table":
                    stmt = stmt.where(
                        ProjectDocument.record_table_id == resolved_record_table_id
                    )
                else:
                    stmt = stmt.where(ProjectDocument.title == target_title)

                existing_result = await session.execute(stmt.limit(1))
                document = existing_result.scalar_one_or_none()
                if document is None:
                    document = ProjectDocument(
                        project_id=resolved_project_id,
                        created_by=user_id,
                        source_type="agent",
                    )
                    session.add(document)

                document.category_id = category_obj.id if category_obj else None
                document.title = target_title[:255]
                document.description = _nullable_text(description)
                document.document_type = _clean_text(document_type, "document")[:64]
                document.target_kind = normalized_target_kind
                document.file_path = (
                    _nullable_text(file_path)
                    if normalized_target_kind == "file"
                    else None
                )
                document.record_table_id = (
                    resolved_record_table_id
                    if normalized_target_kind == "record_table"
                    else None
                )
                document.external_url = (
                    _nullable_text(external_url)
                    if normalized_target_kind == "url"
                    else None
                )
                document.role = _clean_text(role, "reference")[:64]
                document.is_primary = bool(is_primary)
                document.ai_access_level = _one_of(
                    ai_access_level,
                    project_info_ai_access_levels,
                    "metadata",
                )
                document.status = "active"
                document.notes = _nullable_text(notes)
                document.source_ref = _nullable_text(source_ref)
                document.updated_at = datetime.utcnow()
                await session.commit()
                return {"success": True, "document": document.to_dict()}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_register()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @function_tool
    def archive_project_info_category(
        category: str = "",
        category_id: str = "",
        project: str = "",
        project_id: str = "",
    ) -> str:
        """Archive a project information category without deleting its records."""
        from ...memory.database import get_database_manager

        async def _archive():
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
                category_obj = await _resolve_project_info_category(
                    session,
                    resolved_project_id,
                    category,
                    category_id=category_id,
                    create_if_missing=False,
                    user_id=user_id,
                )
                if category_obj is None:
                    raise ValueError("Project information category not found.")
                category_obj.status = "archived"
                category_obj.updated_at = datetime.utcnow()
                await session.commit()
                return {"success": True, "category": category_obj.to_dict()}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_archive()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @function_tool
    def delete_project_document(document_id: str) -> str:
        """Soft-delete a registered project document/link by id."""
        from ...memory.database import get_database_manager
        from ...memory.models import ProjectDocument

        async def _delete():
            db = get_database_manager()
            session = await db.get_session()
            try:
                document = await session.get(ProjectDocument, UUID(document_id))
                if document is None or document.deleted_at is not None:
                    raise ValueError("Project document not found.")
                await _resolve_actor_and_project(
                    session,
                    project_id=str(document.project_id),
                )
                now = datetime.utcnow()
                document.deleted_at = now
                document.updated_at = now
                await session.commit()
                return {"success": True, "document_id": str(document.id)}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_delete()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @function_tool
    def upsert_project_fact(
        title: str,
        content: str,
        project: str = "",
        project_id: str = "",
        category: str = "",
        category_id: str = "",
        fact_id: str = "",
        fact_type: str = "fact",
        confidence: float = 1.0,
        importance: int = 5,
        status: str = "active",
        source_type: str = "agent",
        source_ref: str = "",
        source_document_id: str = "",
        source_task_id: str = "",
    ) -> str:
        """Create or update a durable project fact extracted from tasks, documents, or conversation. Use source_type='conversation' for user-provided facts from the current chat."""
        from ...memory.database import get_database_manager
        from ...memory.models import ProjectFact

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
                await _ensure_project_info_defaults(
                    session,
                    resolved_project_id,
                    user_id,
                )
                category_obj = await _resolve_project_info_category(
                    session,
                    resolved_project_id,
                    category,
                    category_id=category_id,
                    create_if_missing=bool(category or category_id),
                    user_id=user_id,
                    status="active",
                )
                target_title = _clean_text(title)
                target_content = _clean_text(content)
                if not target_title or not target_content:
                    raise ValueError("title and content are required.")

                fact = None
                parsed_fact_id = _parse_optional_uuid(fact_id)
                if parsed_fact_id is not None:
                    fact = await session.get(ProjectFact, parsed_fact_id)
                    if fact and fact.project_id != resolved_project_id:
                        raise ValueError("fact_id belongs to another project.")
                if fact is None:
                    stmt = select(ProjectFact).where(
                        ProjectFact.project_id == resolved_project_id,
                        ProjectFact.deleted_at.is_(None),
                        ProjectFact.title == target_title,
                    )
                    if category_obj:
                        stmt = stmt.where(ProjectFact.category_id == category_obj.id)
                    existing_result = await session.execute(stmt.limit(1))
                    fact = existing_result.scalar_one_or_none()
                if fact is None:
                    fact = ProjectFact(
                        project_id=resolved_project_id,
                        created_by=user_id,
                    )
                    session.add(fact)

                fact.category_id = category_obj.id if category_obj else None
                fact.title = target_title[:255]
                fact.content = target_content
                fact.fact_type = _clean_text(fact_type, "fact")[:64]
                fact.confidence = _clamp_project_info_confidence(confidence)
                fact.importance = _clamp_project_info_importance(importance)
                fact.status = _one_of(status, project_info_item_statuses, "active")
                fact.source_type = _clean_text(source_type, "agent")[:32]
                fact.source_ref = _nullable_text(source_ref)
                fact.source_document_id = _parse_optional_uuid(source_document_id)
                fact.source_task_id = _parse_optional_uuid(source_task_id)
                fact.updated_at = datetime.utcnow()
                await session.commit()
                return {"success": True, "fact": fact.to_dict()}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_upsert()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @function_tool
    def delete_project_fact(fact_id: str) -> str:
        """Soft-delete a durable project fact by id."""
        from ...memory.database import get_database_manager
        from ...memory.models import ProjectFact

        async def _delete():
            db = get_database_manager()
            session = await db.get_session()
            try:
                fact = await session.get(ProjectFact, UUID(fact_id))
                if fact is None or fact.deleted_at is not None:
                    raise ValueError("Project fact not found.")
                await _resolve_actor_and_project(
                    session,
                    project_id=str(fact.project_id),
                )
                now = datetime.utcnow()
                fact.deleted_at = now
                fact.updated_at = now
                await session.commit()
                return {"success": True, "fact_id": str(fact.id)}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_delete()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    @function_tool
    def list_project_tasks_changed_since(
        project: str = "",
        project_id: str = "",
        changed_after: str = "",
        source_type: str = "tasks",
        limit: int = 50,
    ) -> str:
        """List project tasks changed after a timestamp or saved project-info sync cursor."""
        from ...memory.database import get_database_manager
        from ...memory.models import ProjectInfoSyncState, Task

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
                state_result = await session.execute(
                    select(ProjectInfoSyncState).where(
                        ProjectInfoSyncState.project_id == resolved_project_id,
                        ProjectInfoSyncState.source_type == _clean_text(
                            source_type,
                            "tasks",
                        ),
                    )
                )
                sync_state = state_result.scalar_one_or_none()
                checkpoint = _parse_datetime(changed_after)
                if checkpoint is None and sync_state:
                    checkpoint = (
                        sync_state.last_seen_updated_at
                        or sync_state.last_synced_at
                    )

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
                    "sync_state": sync_state.to_dict() if sync_state else None,
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

    @function_tool
    def set_project_information_sync_state(
        project: str = "",
        project_id: str = "",
        source_type: str = "tasks",
        last_seen_updated_at: str = "",
        last_synced_at: str = "",
        metadata_json: str = "",
    ) -> str:
        """Update the project information sync cursor after a successful extraction pass."""
        from ...memory.database import get_database_manager
        from ...memory.models import ProjectInfoSyncState

        async def _set_state():
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
                normalized_source = _clean_text(source_type, "tasks")
                result = await session.execute(
                    select(ProjectInfoSyncState).where(
                        ProjectInfoSyncState.project_id == resolved_project_id,
                        ProjectInfoSyncState.source_type == normalized_source,
                    )
                )
                state = result.scalar_one_or_none()
                if state is None:
                    state = ProjectInfoSyncState(
                        project_id=resolved_project_id,
                        source_type=normalized_source,
                    )
                    session.add(state)

                seen_at = _parse_datetime(last_seen_updated_at)
                synced_at = _parse_datetime(last_synced_at) or datetime.utcnow()
                state.last_synced_at = synced_at
                state.last_seen_updated_at = seen_at or synced_at
                state.updated_at = datetime.utcnow()
                if metadata_json.strip():
                    metadata = json.loads(metadata_json)
                    if not isinstance(metadata, dict):
                        raise ValueError("metadata_json must be a JSON object.")
                    state.sync_metadata = metadata
                await session.commit()
                return {"success": True, "sync_state": state.to_dict()}
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

        try:
            return _json(_run_async(_set_state()))
        except Exception as exc:
            return _json({"success": False, "error": str(exc)})

    return [
        get_project_context,
        list_projects,
        list_project_information,
        organize_project_information_from_folder,
        configure_project_management_files,
        upsert_project_info_category,
        register_project_document,
        archive_project_info_category,
        delete_project_document,
        upsert_project_fact,
        delete_project_fact,
        list_project_tasks_changed_since,
        set_project_information_sync_state,
    ]
