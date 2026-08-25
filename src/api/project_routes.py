"""
Project API routes for AoiTalk Web Interface

Provides endpoints for project management, member management, and join requests.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from os import PathLike
from pathlib import Path, PurePosixPath
from typing import Optional, List, Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4
from fastapi import APIRouter, HTTPException, Request, Depends, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .router_helpers import await_task_completion_before_cancellation
from src.services.turn_context import (
    get_turn_context,
    reset_turn_context,
    set_turn_context,
)
from src.services.project_permissions import PROJECT_PERMISSION_KEYS
from src.services.content_deletion_service import append_event as append_deletion_event
from src.services.project_knowledge_service import (
    ProjectKnowledgeConflict,
    ProjectKnowledgeNotFound,
    attach_project_knowledge_ref,
    remove_project_knowledge_ref,
    resolve_project_knowledge,
)
from ..tools.file_explorer.download_stream import PreparedDownload, remove_temp_download

logger = logging.getLogger(__name__)


class _TemporaryProjectFileResponse(FileResponse):
    """Delete a generated project archive after response streaming."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # FileResponse closes its file before returning or propagating a
            # send error, so synchronous unlink is safe on Windows as well.
            remove_temp_download(Path(self.path))


def _project_download_response(download: PreparedDownload) -> FileResponse:
    """Build a disk-backed response with an explicit UTF-8 filename header."""

    ascii_filename = download.filename.encode("ascii", "replace").decode("ascii")
    ascii_filename = (
        ascii_filename.replace("\\", "_")
        .replace('"', "'")
        .replace("\r", "_")
        .replace("\n", "_")
    )
    encoded_filename = quote(download.filename, safe="")
    content_disposition = (
        f'attachment; filename="{ascii_filename}"; '
        f"filename*=UTF-8''{encoded_filename}"
    )
    response_type = (
        _TemporaryProjectFileResponse if download.temporary else FileResponse
    )
    return response_type(
        path=download.path,
        media_type=download.mime_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": content_disposition,
        },
    )


async def _prepare_project_download_in_threadpool(function, *args):
    """Finish and clean a temp ZIP if request cancellation wins the race."""

    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        current_task = asyncio.current_task()
        uncancel = getattr(current_task, "uncancel", None) if current_task else None
        if callable(uncancel):
            uncancel()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                if callable(uncancel):
                    uncancel()
        try:
            download = worker.result()
        except Exception:
            download = None
        if download is not None and download.temporary:
            remove_temp_download(download.path)
        raise cancellation

# ── Data Models ──────────────────────────────────────────────────────────


class CreateProjectPayload(BaseModel):
    """Payload for creating a new project"""
    name: str
    description: Optional[str] = None
    slug: Optional[str] = None
    aliases: Optional[List[str]] = None
    space_id: Optional[str] = None
    is_completed: bool = False
    allow_join_requests: bool = True
    storage_quota_mb: int = Field(default=1000, ge=0)
    project_metadata: Optional[dict[str, Any]] = None


class UpdateProjectPayload(BaseModel):
    """Payload for updating project settings"""
    name: Optional[str] = None
    description: Optional[str] = None
    aliases: Optional[List[str]] = None
    space_id: Optional[str] = None
    is_completed: Optional[bool] = None
    allow_join_requests: Optional[bool] = None
    storage_quota_mb: Optional[int] = Field(default=None, ge=0)
    project_metadata: Optional[dict[str, Any]] = None


class ProjectKnowledgeRefCreate(BaseModel):
    """Create one explicit Project-to-KnowledgeNode reference."""

    node_id: UUID
    relation_type: Literal["related", "reference", "canonical"] = "related"
    priority: int = Field(default=100, ge=0, le=1_000_000)


class ProjectKnowledgeRefResponse(BaseModel):
    """Compact metadata for a Project Knowledge reference."""

    id: Optional[str] = None
    node_id: str
    title: str
    relation_type: Literal["related", "reference", "canonical"]
    priority: int
    project_id: Optional[str] = None
    docs_library_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None


class ProjectKnowledgeResponse(BaseModel):
    """Canonical and related KnowledgeNode index for one Project."""

    canonical: List[ProjectKnowledgeRefResponse] = Field(default_factory=list)
    related: List[ProjectKnowledgeRefResponse] = Field(default_factory=list)


class AddMemberPayload(BaseModel):
    """Payload for adding a member to project"""
    user_id: Optional[str] = None
    username: Optional[str] = None
    role: str = "member"  # 'admin', 'member', 'viewer'


class UpdateMemberPayload(BaseModel):
    """Payload for updating member permissions"""
    role: Optional[str] = None
    permissions: Optional[dict] = None


class JoinRequestPayload(BaseModel):
    """Payload for submitting join request"""
    message: Optional[str] = None


class ProcessJoinRequestPayload(BaseModel):
    """Payload for approving/rejecting join request"""
    role: str = "member"  # Role to assign if approving


class RejectJoinRequestPayload(BaseModel):
    """Payload for rejecting join request"""
    reason: Optional[str] = None


class ProjectFileCreateFolderPayload(BaseModel):
    """Payload for creating a project folder"""
    path: str = ""
    name: str


class ProjectFileRenamePayload(BaseModel):
    """Payload for renaming a project file or folder"""
    path: str
    new_name: str


class ProjectFileMovePayload(BaseModel):
    """Payload for moving a project file or folder"""
    src: str
    dest: str


class ProjectFileCopyPayload(BaseModel):
    """Payload for copying a project file or folder"""
    src: str
    dest: str


class ProjectFileArchivePayload(BaseModel):
    """Payload for archiving or extracting project files"""
    paths: List[str]
    dest: str = ""


class ProjectFileDownloadPayload(BaseModel):
    """Payload for downloading selected project files or folders."""

    paths: List[str] = Field(..., min_length=1, max_length=100)

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, values: List[str]) -> List[str]:
        if any(len(path) > 4096 for path in values):
            raise ValueError("project file paths are too long")
        return values


class ProjectFileRestorePayload(BaseModel):
    """Payload for restoring a deleted project file or folder"""
    token: str


class ProjectInformationOrganizePayload(BaseModel):
    """Payload for organizing project filer documents into project information."""
    path: str = ""
    apply: bool = False
    use_llm: bool = True
    max_files: int = 80
    draft: Optional[dict[str, Any]] = None


class DailyIntakePayload(BaseModel):
    """Payload for the daily intake (日次インテーク) endpoint."""
    raw_input: str = ""
    intake_date: str = ""
    clarification_answers: str = ""
    apply: bool = False
    use_llm: bool = True
    draft: Optional[dict[str, Any]] = None


def _project_knowledge_item(
    item: dict[str, Any],
    *,
    default_relation_type: str,
) -> dict[str, Any]:
    """Map service metadata to the body-safe API response contract."""

    node = item.get("node") if isinstance(item.get("node"), dict) else item
    relation_type = str(
        item.get("relation_type")
        or node.get("relation_type")
        or default_relation_type
    )
    return {
        "id": item.get("id"),
        "node_id": str(item.get("knowledge_node_id") or node.get("id") or node.get("node_id")),
        "title": str(node.get("title") or "(untitled)"),
        "relation_type": relation_type,
        "priority": int(item.get("priority", node.get("priority", 100))),
        "project_id": item.get("project_id") or node.get("project_id"),
        "docs_library_id": node.get("docs_library_id"),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
    }


def _project_knowledge_response(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "canonical": [
            _project_knowledge_item(item, default_relation_type="canonical")
            for item in payload.get("canonical_nodes", [])
            if isinstance(item, dict)
        ],
        "related": [
            _project_knowledge_item(item, default_relation_type="related")
            for item in payload.get("related_nodes", [])
            if isinstance(item, dict)
        ],
    }


def _raise_project_knowledge_http_error(exc: Exception) -> None:
    if isinstance(exc, ProjectKnowledgeConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ProjectKnowledgeNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


# ── Router Factory ───────────────────────────────────────────────────────


def create_project_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency,
    workspace_root: str | PathLike[str] | None = None,
) -> APIRouter:
    """
    Create the project router with dependencies injected.

    Args:
        get_db_manager: Function to get database manager instance
        get_user_from_request: Function to get current user from request
        require_auth_dependency: Auth dependency for protected routes
        workspace_root: App/Project workspace root。``None`` なら
            ``AOITALK_WORKSPACES_DIR`` 由来の既定 root を使う。削除時のロックと
            実ファイル削除で同じ root を使うため、必ずこの値を透過する。

    Returns:
        APIRouter: Configured router with all project endpoints
    """
    router = APIRouter(prefix="/api/projects", tags=["projects"])
    
    # Import repository lazily to avoid circular imports
    from ..memory.project_repository import ProjectRepository
    from ..memory.user_repository import UserRepository
    from ..services.project_context import (
        ProjectContextResolver,
        has_project_read_access,
        merge_project_metadata,
        normalize_project_metadata,
    )
    from ..tools.file_explorer import (
        get_root_dir as get_workspace_root,
        is_safe_workspace_path,
        list_directory as list_workspace_directory,
        create_directory as create_workspace_directory,
        upload_file_stream as upload_workspace_file_stream,
        resolve_upload_target as resolve_workspace_upload_target,
        rename_item as rename_workspace_item,
        move_item as move_workspace_item,
        copy_item as copy_workspace_item,
        archive_items as archive_workspace_items,
        extract_archives as extract_workspace_archives,
        delete_item as delete_workspace_item,
        restore_from_trash as restore_workspace_item,
        get_file_info as get_workspace_file_info,
        get_preview as get_workspace_file_preview,
        get_full_content as get_workspace_file_content,
        search_files as search_workspace_files,
        resolve_workspace_path as resolve_workspace_file_path,
        workspace_root_context,
    )
    from ..tools.file_explorer.download_stream import (
        prepare_download_items,
        prepare_download_path,
    )
    from ..tools.file_explorer.storage_context import calculate_storage_usage

    effective_workspace_root = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else get_workspace_root().resolve()
    )
    effective_workspace_root.mkdir(parents=True, exist_ok=True)

    def run_workspace_operation(operation, *args, **kwargs):
        """Execute legacy file-explorer I/O against the injected root."""
        with workspace_root_context(effective_workspace_root):
            return operation(*args, **kwargs)

    def trusted_request_session_id(
        request: Request,
        *,
        user_id: UUID,
        project_id: UUID,
    ) -> str | None:
        """Resolve only middleware/turn-owned session identity for usage rows.

        The project path is already authorized below and remains the source of
        truth for project scope.  Caller-controlled headers/query parameters are
        deliberately ignored: accepting an arbitrary session UUID here would
        let a user misattribute usage to another conversation.
        """

        authenticated = str(user_id)
        expected_project = str(project_id)

        def _raw_value(source: Any, *names: str) -> Any:
            if source is None:
                return None
            values = source if isinstance(source, dict) else getattr(source, "__dict__", None)
            if isinstance(values, dict):
                for name in names:
                    raw = values.get(name)
                    if raw:
                        return raw
            for name in names:
                raw = getattr(source, name, None)
                if raw:
                    return raw
            return None

        def _value(source: Any, *names: str) -> str | None:
            raw = _raw_value(source, *names)
            if raw is None:
                return None
            return str(raw).strip() or None

        # A request-state session is trusted only when middleware attached the
        # same authenticated principal and the same authorized project.
        state = getattr(request, "state", None)
        state_user = _raw_value(state, "user", "principal")
        state_user_id = (
            str(state_user).strip()
            if isinstance(state_user, (str, UUID))
            else _value(state_user, "id", "user_id")
        )
        if state_user is not None and state_user_id not in {None, authenticated}:
            state_user_is_trusted = False
        else:
            state_user_is_trusted = True
        state_project = _value(state, "project_id") if state_user_is_trusted else None
        state_session = (
            _value(state, "session_id", "conversation_session_id")
            if state_user_is_trusted and state_project in {None, expected_project}
            else None
        )

        # A task-local turn scope is trusted only when both principal and
        # project match this route.  ``set_turn_context`` below is reset in a
        # finally block so concurrent requests cannot leak identity.
        turn = get_turn_context()
        turn_session = (
            str(turn.session_id).strip()
            if turn.user_id == authenticated
            and turn.project_id in {None, expected_project}
            and turn.session_id
            else None
        )
        return turn_session or state_session

    def normalize_project_member_path(
        storage_root: str,
        path: Optional[str],
        *,
        allow_root: bool = True,
    ) -> str:
        clean = (path or "").replace("\\", "/").strip("/")
        if not clean:
            if not allow_root:
                raise HTTPException(
                    status_code=400,
                    detail="Project storage root cannot be modified",
                )
            return storage_root

        parts = PurePosixPath(clean).parts
        if not parts:
            if not allow_root:
                raise HTTPException(
                    status_code=400,
                    detail="Project storage root cannot be modified",
                )
            return storage_root
        if any(part in {"..", ""} for part in parts):
            raise HTTPException(status_code=400, detail="Invalid project file path")

        return f"{storage_root}/{'/'.join(parts)}"

    def strip_project_storage_prefix(storage_root: str, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None

        normalized_root = storage_root.replace("\\", "/").strip("/")
        normalized_value = value.replace("\\", "/").strip("/")

        if not normalized_value or normalized_value == normalized_root:
            return ""
        prefix = f"{normalized_root}/"
        if normalized_value.startswith(prefix):
            return normalized_value[len(prefix):]
        return normalized_value

    def serialize_project_file_listing(storage_root: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            **result,
            "root_path": "",
            "current_path": strip_project_storage_prefix(storage_root, result.get("current_path")) or "",
            "parent_path": strip_project_storage_prefix(storage_root, result.get("parent_path")),
            "directories": [
                {
                    **item,
                    "path": strip_project_storage_prefix(storage_root, item.get("path")) or "",
                }
                for item in result.get("directories", [])
            ],
            "files": [
                {
                    **item,
                    "path": strip_project_storage_prefix(storage_root, item.get("path")) or "",
                }
                for item in result.get("files", [])
            ],
        }

    def ensure_project_storage_root(storage_root: str):
        workspace_base = effective_workspace_root
        relative_storage_root = Path(storage_root)
        if relative_storage_root.is_absolute() or ".." in relative_storage_root.parts:
            raise HTTPException(status_code=400, detail="Invalid project storage path")
        lexical_target = workspace_base / relative_storage_root
        if not is_safe_workspace_path(workspace_base, lexical_target):
            raise HTTPException(
                status_code=400,
                detail="Project storage path must not cross a symlink",
            )
        target = lexical_target
        if not is_safe_workspace_path(workspace_base, target):
            raise HTTPException(
                status_code=400,
                detail="Project storage path escapes the workspace root",
            )
        target.mkdir(parents=True, exist_ok=True)
        if not is_safe_workspace_path(workspace_base, target):
            raise HTTPException(
                status_code=400,
                detail="Project storage path changed outside the workspace root",
            )
        return target

    async def ensure_assignable_member_role(
        session,
        *,
        project_id: UUID,
        actor_id: UUID,
        actor_role: Optional[str],
        role: str,
    ) -> None:
        if role not in {"admin", "member", "viewer"}:
            raise HTTPException(
                status_code=400,
                detail="role must be one of: admin, member, viewer",
            )
        if role != "admin":
            return
        project = await ProjectRepository.get_by_id(session, project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        actor = await UserRepository.get_by_id(session, actor_id)
        is_global_admin = getattr(actor, "role", None) == "admin"
        if not is_global_admin and project.owner_id != actor_id:
            raise HTTPException(
                status_code=403,
                detail="Only the project owner or global admin may assign admin role",
            )
    
    # ── Project CRUD ─────────────────────────────────────────────────────
    
    @router.post("")
    async def create_project(
        payload: CreateProjectPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Create a new project"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                space_id = None
                if payload.space_id:
                    try:
                        space_id = UUID(payload.space_id)
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail="Invalid space_id") from exc
                    from ..services.space_access import can_write_space

                    can_write, space = await can_write_space(
                        session,
                        space_id=space_id,
                        user_id=UUID(user_info["id"]),
                        user_info=user_info,
                    )
                    if space is None:
                        raise HTTPException(status_code=404, detail="Space not found")
                    if not can_write:
                        raise HTTPException(status_code=403, detail="Space access denied")
                project = await ProjectRepository.create_project(
                    session,
                    owner_id=UUID(user_info["id"]),
                    name=payload.name,
                    description=payload.description,
                    slug=payload.slug,
                    aliases=payload.aliases,
                    space_id=space_id,
                    is_completed=payload.is_completed,
                    allow_join_requests=payload.allow_join_requests,
                    storage_quota_mb=payload.storage_quota_mb,
                    project_metadata=normalize_project_metadata(payload.project_metadata)
                    if payload.project_metadata is not None
                    else None,
                )

                # Initialize the canonical Project information node in the
                # owner's Personal Docs Library. The repository create path
                # commits its project/member rows internally, so this repair
                # is intentionally idempotent and can be retried from the
                # Project tab if a transient Docs error occurs.
                from ..services.project_information_docs import (
                    ensure_project_information_doc,
                    is_default_inbox_project,
                )
                if not is_default_inbox_project(project):
                    await ensure_project_information_doc(
                        session,
                        project=project,
                        user_id=UUID(user_info["id"]),
                    )
                    await session.commit()
                    await session.refresh(project)
                
                # ワークスペースディレクトリを即座に作成
                storage_root = await ProjectRepository.get_storage_path(project.id)
                ensure_project_storage_root(storage_root)

                logger.info(f"Project created: {project.name} (slug: {project.slug}) by {user_info['username']}")
                return JSONResponse({
                    "success": True,
                    "project": project.to_dict()
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to create project: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("")
    async def list_projects(
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """List all projects the current user has access to"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                user_id = UUID(user_info["id"])
                await ProjectRepository.ensure_user_inbox_setup(session, user_id)
                await session.commit()
                projects = await ProjectRepository.get_user_projects(
                    session,
                    user_id=user_id
                )
                return JSONResponse({
                    "projects": projects,
                    "total": len(projects)
                })
            finally:
                await session.close()
        except Exception as e:
            logger.error(f"Failed to list projects: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/{project_id}")
    async def get_project(
        project_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Get project details"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                project = await ProjectRepository.get_by_id(
                    session,
                    project_id=UUID(project_id),
                    include_members=True
                )
                
                if not project:
                    raise HTTPException(status_code=404, detail="Project not found")
                
                if not await has_project_read_access(
                    session,
                    project,
                    user_id=user_info["id"],
                    user_role=user_info.get("role"),
                ):
                    raise HTTPException(status_code=403, detail="Access denied")

                member = await ProjectRepository.get_member(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"])
                )
                
                result = project.to_dict()
                result["is_member"] = member is not None
                if member:
                    result["membership"] = member.to_dict()
                
                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get project: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get(
        "/{project_id}/knowledge",
        response_model=ProjectKnowledgeResponse,
    )
    async def get_project_knowledge(
        project_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """List canonical and ACL-visible shared KnowledgeNode references."""

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            payload = await resolve_project_knowledge(
                project_id=project_id,
                actor_user_id=user_info["id"],
            )
            return _project_knowledge_response(payload)
        except HTTPException:
            raise
        except Exception as exc:
            _raise_project_knowledge_http_error(exc)

    @router.post(
        "/{project_id}/knowledge",
        response_model=ProjectKnowledgeRefResponse,
        status_code=201,
    )
    async def add_project_knowledge(
        project_id: str,
        payload: ProjectKnowledgeRefCreate,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Attach one ACL-visible KnowledgeNode to a Project."""

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            result = await attach_project_knowledge_ref(
                project_id=project_id,
                knowledge_node_id=payload.node_id,
                relation_type=payload.relation_type,
                priority=payload.priority,
                actor_user_id=user_info["id"],
            )
            return _project_knowledge_item(
                result,
                default_relation_type=payload.relation_type,
            )
        except HTTPException:
            raise
        except Exception as exc:
            _raise_project_knowledge_http_error(exc)

    @router.delete(
        "/{project_id}/knowledge/{node_id}",
    )
    async def delete_project_knowledge(
        project_id: str,
        node_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Detach one Project Knowledge reference without changing the node."""

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            return await remove_project_knowledge_ref(
                project_id=project_id,
                knowledge_node_id=node_id,
                actor_user_id=user_info["id"],
            )
        except HTTPException:
            raise
        except Exception as exc:
            _raise_project_knowledge_http_error(exc)
    
    @router.patch("/{project_id}")
    async def update_project(
        project_id: str,
        payload: UpdateProjectPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Update project settings (owner/admin only)"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                # Check permission
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="manage_settings"
                )
                
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")
                
                # Update
                update_data = payload.model_dump(exclude_unset=True)
                if "storage_quota_mb" in update_data and update_data["storage_quota_mb"] is None:
                    raise HTTPException(
                        status_code=400,
                        detail="storage_quota_mb must be a non-negative integer",
                    )
                if "project_metadata" in update_data:
                    existing_project = await ProjectRepository.get_by_id(
                        session,
                        UUID(project_id),
                    )
                    if not existing_project:
                        raise HTTPException(status_code=404, detail="Project not found")
                    update_data["project_metadata"] = merge_project_metadata(
                        existing_project.project_metadata,
                        update_data["project_metadata"],
                    )
                if "space_id" in update_data and update_data["space_id"]:
                    try:
                        update_data["space_id"] = UUID(update_data["space_id"])
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail="Invalid space_id") from exc
                    from ..services.space_access import can_write_space

                    can_write, space = await can_write_space(
                        session,
                        space_id=update_data["space_id"],
                        user_id=UUID(user_info["id"]),
                        user_info=user_info,
                    )
                    if space is None:
                        raise HTTPException(status_code=404, detail="Space not found")
                    if not can_write:
                        raise HTTPException(status_code=403, detail="Space access denied")
                elif "space_id" in update_data:
                    update_data["space_id"] = None

                project = await ProjectRepository.update_project(
                    session,
                    project_id=UUID(project_id),
                    **update_data
                )
                
                if not project:
                    raise HTTPException(status_code=404, detail="Project not found")
                
                return JSONResponse({
                    "success": True,
                    "project": project.to_dict()
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update project: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.delete("/{project_id}")
    async def delete_project(
        project_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Delete a project (owner only)"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                # Get project to check ownership
                project = await ProjectRepository.get_by_id(session, UUID(project_id))
                if not project:
                    raise HTTPException(status_code=404, detail="Project not found")
                
                if (
                    str(project.owner_id) != user_info["id"]
                    and user_info.get("role") != "admin"
                ):
                    raise HTTPException(status_code=403, detail="Only owner can delete project")
                
                deleted = await ProjectRepository.delete_project(
                    session,
                    UUID(project_id),
                    delete_workspace=True,
                    workspace_root=workspace_root,
                )
                return JSONResponse({
                    "success": deleted,
                    "message": "Project deleted" if deleted else "Failed to delete"
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete project: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    # ── Member Management ────────────────────────────────────────────────

    @router.get("/{project_id}/assignee-candidates")
    async def list_assignee_candidates(
        project_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """List the minimal active-member fields needed for task assignment."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="write",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                members = await ProjectRepository.get_project_assignee_candidates(
                    session,
                    project_id=UUID(project_id),
                )
                return JSONResponse({"members": members, "total": len(members)})
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to list assignee candidates: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/{project_id}/members")
    async def list_members(
        project_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """List all members of a project"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="manage_members",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")
                
                members = await ProjectRepository.get_project_members(
                    session,
                    project_id=UUID(project_id)
                )
                
                return JSONResponse({
                    "members": members,
                    "total": len(members)
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to list members: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/{project_id}/members")
    async def add_member(
        project_id: str,
        payload: AddMemberPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Add a member to project (admin/owner only)"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                # Check permission
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="manage_members"
                )
                
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")
                
                resolved_user_id = payload.user_id
                if not resolved_user_id and payload.username:
                    lookup_user = await UserRepository.get_by_username(session, payload.username)
                    if not lookup_user:
                        raise HTTPException(status_code=404, detail="User not found")
                    resolved_user_id = str(lookup_user.id)

                if not resolved_user_id:
                    raise HTTPException(status_code=400, detail="user_id or username is required")

                await ensure_assignable_member_role(
                    session,
                    project_id=UUID(project_id),
                    actor_id=UUID(user_info["id"]),
                    actor_role=user_info.get("role"),
                    role=payload.role,
                )

                member = await ProjectRepository.add_member(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(resolved_user_id),
                    role=payload.role,
                    invited_by=UUID(user_info["id"])
                )
                
                if not member:
                    raise HTTPException(status_code=400, detail="User is already a member")
                
                return JSONResponse({
                    "success": True,
                    "member": member.to_dict()
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to add member: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.patch("/{project_id}/members/{user_id}")
    async def update_member(
        project_id: str,
        user_id: str,
        payload: UpdateMemberPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Update member role/permissions"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        current_user = await get_user_from_request(request)
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                # Check permission
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(current_user["id"]),
                    permission="manage_members"
                )
                
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                target_user_id = UUID(user_id)
                target_member = await ProjectRepository.get_member(
                    session,
                    project_id=UUID(project_id),
                    user_id=target_user_id,
                )
                if not target_member:
                    raise HTTPException(status_code=404, detail="Member not found")
                project = await ProjectRepository.get_by_id(session, UUID(project_id))
                if project and project.owner_id == target_user_id:
                    raise HTTPException(status_code=403, detail="Project owner cannot be modified")

                if payload.permissions is not None:
                    actor_is_project_owner = bool(
                        project and project.owner_id == UUID(current_user["id"])
                    )
                    if current_user.get("role") != "admin" and not actor_is_project_owner:
                        raise HTTPException(
                            status_code=403,
                            detail="Only the project owner or global admin may change permissions",
                        )
                    unknown_keys = set(payload.permissions) - set(PROJECT_PERMISSION_KEYS)
                    if unknown_keys or not all(
                        isinstance(key, str) and isinstance(value, bool)
                        for key, value in payload.permissions.items()
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="permissions values must be boolean",
                        )

                if payload.role is not None:
                    await ensure_assignable_member_role(
                        session,
                        project_id=UUID(project_id),
                        actor_id=UUID(current_user["id"]),
                        actor_role=current_user.get("role"),
                        role=payload.role,
                    )
                
                member = await ProjectRepository.update_member(
                    session,
                    project_id=UUID(project_id),
                    user_id=target_user_id,
                    role=payload.role,
                    permissions=payload.permissions
                )
                
                if not member:
                    raise HTTPException(status_code=404, detail="Member not found")
                
                return JSONResponse({
                    "success": True,
                    "member": member.to_dict()
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update member: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.delete("/{project_id}/members/{user_id}")
    async def remove_member(
        project_id: str,
        user_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Remove a member from project"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        current_user = await get_user_from_request(request)
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                # Check permission (or self-removal)
                is_self = user_id == current_user["id"]
                
                if not is_self:
                    has_perm = await ProjectRepository.has_permission(
                        session,
                        project_id=UUID(project_id),
                        user_id=UUID(current_user["id"]),
                        permission="manage_members"
                    )
                    
                    if not has_perm:
                        raise HTTPException(status_code=403, detail="Permission denied")

                project = await ProjectRepository.get_by_id(session, UUID(project_id))
                if project and project.owner_id == UUID(user_id):
                    raise HTTPException(status_code=403, detail="Project owner cannot be removed")
                
                removed = await ProjectRepository.remove_member(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_id)
                )
                
                if not removed:
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot remove owner or member not found"
                    )
                
                return JSONResponse({
                    "success": True,
                    "message": "Member removed"
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to remove member: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    # ── Join Requests ────────────────────────────────────────────────────
    
    @router.post("/{project_id}/join-requests")
    async def submit_join_request(
        project_id: str,
        payload: JoinRequestPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Submit a request to join a project"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                # Check if project accepts join requests
                project = await ProjectRepository.get_by_id(session, UUID(project_id))
                if not project:
                    raise HTTPException(status_code=404, detail="Project not found")
                
                if not project.allow_join_requests:
                    raise HTTPException(status_code=400, detail="Project does not accept join requests")
                
                join_request = await ProjectRepository.create_join_request(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    message=payload.message
                )
                
                if not join_request:
                    raise HTTPException(
                        status_code=400,
                        detail="Already a member or pending request exists"
                    )
                
                return JSONResponse({
                    "success": True,
                    "request": join_request.to_dict()
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to submit join request: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/{project_id}/join-requests")
    async def list_join_requests(
        project_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """List pending join requests (admin/owner only)"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                # Check permission
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="manage_members"
                )
                
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                requests = await ProjectRepository.get_pending_requests(
                    session,
                    project_id=UUID(project_id)
                )
                
                return JSONResponse({
                    "requests": requests,
                    "total": len(requests)
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to list join requests: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/{project_id}/join-requests/{request_id}/approve")
    async def approve_join_request(
        project_id: str,
        request_id: str,
        payload: ProcessJoinRequestPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Approve a join request"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                # Check permission
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="manage_members"
                )
                
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                await ensure_assignable_member_role(
                    session,
                    project_id=UUID(project_id),
                    actor_id=UUID(user_info["id"]),
                    actor_role=user_info.get("role"),
                    role=payload.role,
                )
                
                member = await ProjectRepository.approve_join_request(
                    session,
                    request_id=UUID(request_id),
                    project_id=UUID(project_id),
                    approved_by=UUID(user_info["id"]),
                    role=payload.role
                )
                
                if not member:
                    raise HTTPException(status_code=400, detail="Request not found or already processed")
                
                return JSONResponse({
                    "success": True,
                    "member": member.to_dict()
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to approve join request: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/{project_id}/join-requests/{request_id}/reject")
    async def reject_join_request(
        project_id: str,
        request_id: str,
        payload: RejectJoinRequestPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Reject a join request"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                # Check permission
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="manage_members"
                )
                
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")
                
                rejected = await ProjectRepository.reject_join_request(
                    session,
                    request_id=UUID(request_id),
                    project_id=UUID(project_id),
                    rejected_by=UUID(user_info["id"]),
                    reason=payload.reason
                )
                
                if not rejected:
                    raise HTTPException(status_code=400, detail="Request not found or already processed")
                
                return JSONResponse({
                    "success": True,
                    "message": "Request rejected"
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to reject join request: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{project_id}/files")
    async def list_project_files(
        project_id: str,
        request: Request,
        path: str = "",
        _: None = Depends(require_auth_dependency),
    ):
        """List files inside the project's storage root."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="read",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                result = run_workspace_operation(
                    list_workspace_directory,
                    normalize_project_member_path(storage_root, path),
                )
                if not result.get("success"):
                    raise HTTPException(status_code=400, detail=result.get("error", "Failed to list project files"))
                return JSONResponse(serialize_project_file_listing(storage_root, result))
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to list project files: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{project_id}/files/info")
    async def get_project_file_info(
        project_id: str,
        request: Request,
        path: str,
        _: None = Depends(require_auth_dependency),
    ):
        """Get metadata for a file or folder inside project storage."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="read",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                result = run_workspace_operation(
                    get_workspace_file_info,
                    normalize_project_member_path(storage_root, path),
                )
                if not result.get("success"):
                    raise HTTPException(status_code=404, detail=result.get("error", "Project file not found"))
                result["path"] = strip_project_storage_prefix(storage_root, result.get("path")) or ""
                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get project file info: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{project_id}/files/preview")
    async def preview_project_file(
        project_id: str,
        request: Request,
        path: str,
        _: None = Depends(require_auth_dependency),
    ):
        """Preview a project file."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="read",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                result = run_workspace_operation(
                    get_workspace_file_preview,
                    normalize_project_member_path(storage_root, path),
                )
                if not result.get("success"):
                    raise HTTPException(status_code=400, detail=result.get("error", "Failed to preview project file"))
                result["path"] = strip_project_storage_prefix(storage_root, result.get("path")) or ""
                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to preview project file: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{project_id}/files/content")
    async def get_project_file_content(
        project_id: str,
        request: Request,
        path: str,
        _: None = Depends(require_auth_dependency),
    ):
        """Read text content from a project file without exposing its absolute path."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="read",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")
                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                result = run_workspace_operation(
                    get_workspace_file_content,
                    normalize_project_member_path(storage_root, path),
                )
                if not result.get("success"):
                    raise HTTPException(status_code=400, detail=result.get("error", "Failed to read file"))
                result["path"] = strip_project_storage_prefix(storage_root, result.get("path")) or ""
                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to read project file: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{project_id}/files/search")
    async def search_project_files(
        project_id: str,
        request: Request,
        q: str,
        path: str = "",
        limit: int = 50,
        _: None = Depends(require_auth_dependency),
    ):
        """Search file names within a project-scoped workspace only."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="read",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")
                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                result = run_workspace_operation(
                    search_workspace_files,
                    q,
                    normalize_project_member_path(storage_root, path),
                    max_results=max(1, min(limit, 200)),
                )
                result["root_path"] = ""
                for item in result.get("results", []):
                    item["path"] = strip_project_storage_prefix(storage_root, item.get("path")) or ""
                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to search project files: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{project_id}/files/folders")
    async def create_project_folder(
        project_id: str,
        payload: ProjectFileCreateFolderPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Create a folder inside project storage."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="write",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                result = run_workspace_operation(
                    create_workspace_directory,
                    normalize_project_member_path(storage_root, payload.path),
                    payload.name,
                )
                if not result.get("success"):
                    raise HTTPException(status_code=400, detail=result.get("error", "Failed to create folder"))
                if result.get("path"):
                    result["path"] = strip_project_storage_prefix(storage_root, result.get("path")) or ""
                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to create project folder: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{project_id}/files/upload")
    async def upload_project_file(
        project_id: str,
        request: Request,
        file: UploadFile = File(...),
        path: str = "",
        _: None = Depends(require_auth_dependency),
    ):
        """Upload a file into project storage."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="write",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                await file.seek(0)

                project = await ProjectRepository.get_by_id_for_update(
                    session, UUID(project_id)
                )
                if not project or project.deleted_at is not None:
                    raise HTTPException(status_code=404, detail="Project not found")

                # The initial check protects the cheap/read path.  Recheck
                # after the project lock so a membership change that won the
                # race cannot be bypassed by this filesystem writer.
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="write",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                storage_path = ensure_project_storage_root(storage_root)
                upload_directory = normalize_project_member_path(storage_root, path)
                upload_filename = file.filename or "unnamed_file"
                upload_target, upload_target_error = run_workspace_operation(
                    resolve_workspace_upload_target,
                    upload_directory,
                    upload_filename,
                )
                if upload_target is None:
                    raise HTTPException(
                        status_code=400,
                        detail=upload_target_error or "Invalid upload path",
                    )

                async def persist_upload():
                    created_path = None
                    backup_path = None
                    try:
                        if upload_target.is_file():
                            backup_root = effective_workspace_root / ".project-upload-backups"
                            backup_root.mkdir(parents=True, exist_ok=True)
                            fd, backup_name = tempfile.mkstemp(
                                prefix=f"project-{project_id}-",
                                suffix=".tmp",
                                dir=backup_root,
                            )
                            os.close(fd)
                            backup_candidate = Path(backup_name)
                            backup_candidate.unlink()
                            try:
                                try:
                                    os.link(upload_target, backup_candidate)
                                except OSError:
                                    await asyncio.to_thread(
                                        shutil.copy2,
                                        upload_target,
                                        backup_candidate,
                                    )
                            except Exception:
                                backup_candidate.unlink(missing_ok=True)
                                raise
                            backup_path = backup_candidate
                        result = await asyncio.to_thread(
                            run_workspace_operation,
                            upload_workspace_file_stream,
                            upload_directory,
                            upload_filename,
                            file.file,
                            allow_overwrite=True,
                        )
                        if not result.get("success"):
                            raise HTTPException(
                                status_code=400,
                                detail=result.get("error", "Failed to upload file"),
                            )
                        raw_created_path = result.get("path")
                        if raw_created_path:
                            created_path = Path(str(raw_created_path))
                            if not created_path.is_absolute():
                                created_path = effective_workspace_root / created_path
                            if not is_safe_workspace_path(storage_path, created_path):
                                raise HTTPException(
                                    status_code=400,
                                    detail="Invalid uploaded project path",
                                )
                            created_path = created_path.resolve(strict=False)
                        final_usage = await asyncio.to_thread(
                            calculate_storage_usage,
                            storage_path,
                            strict=True,
                        )
                        project.storage_used_mb = final_usage["total_bytes"] / (
                            1024 * 1024
                        )
                        await session.commit()
                        if backup_path is not None:
                            try:
                                backup_path.unlink(missing_ok=True)
                            except OSError:
                                logger.warning(
                                    "Failed to remove project upload backup: %s",
                                    backup_path,
                                )
                        backup_path = None
                        created_path = None
                        if result.get("path"):
                            result["path"] = strip_project_storage_prefix(
                                storage_root, result.get("path")
                            ) or ""
                        return JSONResponse(result)
                    except Exception:
                        try:
                            await session.rollback()
                        except Exception:
                            logger.exception("Failed to roll back project upload metadata")
                        finally:
                            if backup_path is not None and backup_path.exists():
                                try:
                                    os.replace(backup_path, upload_target)
                                except OSError:
                                    logger.exception(
                                        "Failed to restore overwritten project upload: %s",
                                        upload_target,
                                    )
                            elif created_path:
                                created_candidate = Path(str(created_path))
                                if is_safe_workspace_path(storage_path, created_candidate):
                                    try:
                                        created_candidate.unlink(missing_ok=True)
                                    except OSError:
                                        logger.warning(
                                            "Failed to remove orphaned project upload: %s",
                                            created_candidate,
                                        )
                        raise

                return await await_task_completion_before_cancellation(
                    persist_upload()
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to upload project file: {e}")
            try:
                from ..services.failure_recorder import record_failure_event

                await record_failure_event(
                    source="backend",
                    operation="project_file_upload",
                    project_id=project_id,
                    error=e,
                    input_summary={"project_id": project_id, "filename": file.filename},
                )
            except Exception:
                logger.debug("Failed to record project file upload failure", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{project_id}/files/rename")
    async def rename_project_file(
        project_id: str,
        payload: ProjectFileRenamePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Rename a file or folder inside project storage."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="write",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                result = run_workspace_operation(
                    rename_workspace_item,
                    normalize_project_member_path(
                        storage_root, payload.path, allow_root=False
                    ),
                    payload.new_name,
                )
                if not result.get("success"):
                    raise HTTPException(status_code=400, detail=result.get("error", "Failed to rename file"))
                if result.get("new_path"):
                    result["new_path"] = strip_project_storage_prefix(storage_root, result.get("new_path")) or ""
                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to rename project file: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{project_id}/files/move")
    async def move_project_file(
        project_id: str,
        payload: ProjectFileMovePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Move a file or folder inside project storage."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="write",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                result = run_workspace_operation(
                    move_workspace_item,
                    normalize_project_member_path(
                        storage_root, payload.src, allow_root=False
                    ),
                    normalize_project_member_path(storage_root, payload.dest),
                )
                if not result.get("success"):
                    raise HTTPException(
                        status_code=400,
                        detail=result.get("error", "Failed to move file"),
                    )
                if result.get("new_path"):
                    result["new_path"] = strip_project_storage_prefix(
                        storage_root, result.get("new_path")
                    ) or ""
                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to move project file: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    async def run_project_file_operation(
        project_id: str,
        request: Request,
        operation,
        *paths: str,
    ):
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        session = await db_manager.get_session()
        try:
            has_perm = await ProjectRepository.has_permission(
                session,
                project_id=UUID(project_id),
                user_id=UUID(user_info["id"]),
                permission="write",
            )
            if not has_perm:
                raise HTTPException(status_code=403, detail="Permission denied")
            storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
            ensure_project_storage_root(storage_root)
            normalized_paths = [
                normalize_project_member_path(
                    storage_root,
                    path,
                    allow_root=index != 0,
                )
                for index, path in enumerate(paths)
            ]
            result = await asyncio.to_thread(
                run_workspace_operation,
                operation,
                *normalized_paths,
            )
            return result, storage_root
        finally:
            await session.close()

    @router.post("/{project_id}/files/copy")
    async def copy_project_file(
        project_id: str,
        payload: ProjectFileCopyPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Copy a file or folder inside project storage."""
        try:
            result, storage_root = await run_project_file_operation(
                project_id, request, copy_workspace_item, payload.src, payload.dest
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("error", "Failed to copy file"),
                )
            if result.get("new_path"):
                result["new_path"] = strip_project_storage_prefix(
                    storage_root, result.get("new_path")
                ) or ""
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to copy project file: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{project_id}/files/archive")
    async def archive_project_files(
        project_id: str,
        payload: ProjectFileArchivePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Create an archive from files inside project storage."""
        try:
            db_manager = get_db_manager()
            if db_manager is None:
                raise HTTPException(status_code=503, detail="Database not available")
            user_info = await get_user_from_request(request)
            if not user_info:
                raise HTTPException(status_code=401, detail="Not authenticated")
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="write",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")
                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                paths = [
                    normalize_project_member_path(
                        storage_root, path, allow_root=False
                    )
                    for path in payload.paths
                ]
                dest = normalize_project_member_path(storage_root, payload.dest)
                result = await asyncio.to_thread(
                    run_workspace_operation,
                    archive_workspace_items,
                    paths,
                    dest,
                )
            finally:
                await session.close()
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("error", "Failed to archive files"),
                )
            if result.get("archive_path"):
                result["archive_path"] = strip_project_storage_prefix(
                    storage_root, result.get("archive_path")
                ) or ""
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to archive project files: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{project_id}/files/extract")
    async def extract_project_files(
        project_id: str,
        payload: ProjectFileArchivePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Extract archives inside project storage."""
        try:
            db_manager = get_db_manager()
            if db_manager is None:
                raise HTTPException(status_code=503, detail="Database not available")
            user_info = await get_user_from_request(request)
            if not user_info:
                raise HTTPException(status_code=401, detail="Not authenticated")
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="write",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")
                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                paths = [
                    normalize_project_member_path(
                        storage_root, path, allow_root=False
                    )
                    for path in payload.paths
                ]
                dest = normalize_project_member_path(storage_root, payload.dest)
                result = await asyncio.to_thread(
                    run_workspace_operation,
                    extract_workspace_archives,
                    paths,
                    dest,
                )
            finally:
                await session.close()
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("error", "Failed to extract archives"),
                )
            for item in result.get("extracted", []):
                if isinstance(item, dict) and item.get("path"):
                    item["path"] = strip_project_storage_prefix(
                        storage_root, item.get("path")
                    ) or ""
            return JSONResponse(result)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to extract project files: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{project_id}/files/restore")
    async def restore_project_file(
        project_id: str,
        payload: ProjectFileRestorePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Restore a deleted file or folder inside project storage."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                project = await ProjectRepository.get_by_id_for_update(
                    session, UUID(project_id)
                )
                if not project or project.deleted_at is not None:
                    raise HTTPException(status_code=404, detail="Project not found")
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="delete",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")
                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                storage_path = ensure_project_storage_root(storage_root)
                result = run_workspace_operation(
                    restore_workspace_item,
                    payload.token,
                    allowed_root=storage_root,
                )
                if not result.get("success"):
                    status_code = 403 if result.get("code") == "forbidden" else 404
                    raise HTTPException(
                        status_code=status_code,
                        detail=result.get("error", "Failed to restore file"),
                    )
                await append_deletion_event(
                    session,
                    "workspace_file",
                    str(payload.token),
                    action="restored",
                    root_entity_id=str(payload.token),
                    batch_id=uuid4(),
                    project_id=UUID(project_id),
                    actor_user_id=UUID(user_info["id"]),
                    source="web.project.files.restore",
                    metadata={"trash_token": str(payload.token)},
                )
                usage = await asyncio.to_thread(
                    calculate_storage_usage,
                    storage_path,
                    strict=True,
                )
                project.storage_used_mb = usage["total_bytes"] / (1024 * 1024)
                await session.commit()
                if result.get("restored_path"):
                    result["restored_path"] = strip_project_storage_prefix(
                        storage_root, result.get("restored_path")
                    ) or ""
                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to restore project file: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/{project_id}/files")
    async def delete_project_file(
        project_id: str,
        request: Request,
        path: str,
        _: None = Depends(require_auth_dependency),
    ):
        """Delete a file or folder inside project storage."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                project = await ProjectRepository.get_by_id_for_update(
                    session, UUID(project_id)
                )
                if not project or project.deleted_at is not None:
                    raise HTTPException(status_code=404, detail="Project not found")

                # Recheck under the same project row lock used by writers so a
                # concurrent ACL change cannot authorize this filesystem delete.
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="delete",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                storage_path = ensure_project_storage_root(storage_root)
                result = run_workspace_operation(
                    delete_workspace_item,
                    normalize_project_member_path(
                        storage_root, path, allow_root=False
                    ),
                    require_trash=True,
                )
                if not result.get("success"):
                    raise HTTPException(status_code=400, detail=result.get("error", "Failed to delete file"))

                trash = result.get("trash")
                trash_token = trash.get("token") if isinstance(trash, dict) else None
                if not trash_token:
                    raise RuntimeError("Project file deletion did not create a recovery token")

                try:
                    await append_deletion_event(
                        session,
                        "workspace_file",
                        normalize_project_member_path(
                            storage_root, path, allow_root=False
                        ),
                        action="deleted",
                        root_entity_id=path,
                        batch_id=uuid4(),
                        project_id=UUID(project_id),
                        actor_user_id=UUID(user_info["id"]),
                        source="web.project.files.delete",
                        metadata={
                            "trash_token": str(trash_token),
                            "delete_mode": "trash",
                        },
                    )
                    usage = await asyncio.to_thread(
                        calculate_storage_usage,
                        storage_path,
                        strict=True,
                    )
                    project.storage_used_mb = usage["total_bytes"] / (1024 * 1024)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    restored = run_workspace_operation(
                        restore_workspace_item,
                        trash_token,
                    )
                    if not restored.get("success"):
                        logger.critical(
                            "Failed to restore project file after storage transaction rollback: "
                            "project=%s path=%s token=%s error=%s",
                            project_id,
                            path,
                            trash_token,
                            restored.get("error"),
                        )
                    raise
                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete project file: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{project_id}/files/download")
    async def download_project_file(
        project_id: str,
        request: Request,
        path: str,
        _: None = Depends(require_auth_dependency),
    ):
        """Download a file from project storage."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="read",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                normalized_path = normalize_project_member_path(storage_root, path)
                resolved_path, valid = run_workspace_operation(
                    resolve_workspace_file_path,
                    normalized_path,
                )
                if not valid:
                    raise HTTPException(status_code=404, detail="File not found")
                download = await _prepare_project_download_in_threadpool(
                    prepare_download_path,
                    resolved_path,
                )
                if download is None:
                    raise HTTPException(status_code=404, detail="File not found")

                return _project_download_response(download)
            finally:
                await session.close()
        except HTTPException:
            raise
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid project id") from exc
        except Exception as e:
            logger.error(f"Failed to download project file: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.head("/{project_id}/files/download", include_in_schema=False)
    async def head_project_file(
        project_id: str,
        request: Request,
        path: str,
        _: None = Depends(require_auth_dependency),
    ):
        """Return project download headers without transferring the body."""

        return await download_project_file(project_id, request, path, None)

    @router.post("/{project_id}/files/download")
    async def download_project_files(
        project_id: str,
        payload: ProjectFileDownloadPayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Download selected project files or folders as a disk-backed ZIP."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                project_uuid = UUID(project_id)
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=project_uuid,
                    user_id=UUID(user_info["id"]),
                    permission="read",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(project_uuid)
                ensure_project_storage_root(storage_root)
                normalized_paths = [
                    normalize_project_member_path(storage_root, value)
                    for value in payload.paths
                ]
                download = await _prepare_project_download_in_threadpool(
                    run_workspace_operation,
                    prepare_download_items,
                    normalized_paths,
                )
                if download is None:
                    raise HTTPException(status_code=404, detail="File not found")

                return _project_download_response(download)
            finally:
                await session.close()
        except HTTPException:
            raise
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid project id") from exc
        except Exception as e:
            logger.error(f"Failed to download selected project files: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{project_id}/storage-usage")
    async def get_project_storage_usage(
        project_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Return storage usage for project workspace."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                project_uuid = UUID(project_id)
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=project_uuid,
                    user_id=UUID(user_info["id"]),
                    permission="read",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Access denied")

                project = await ProjectRepository.get_by_id(session, project_uuid)
                if not project:
                    raise HTTPException(status_code=404, detail="Project not found")

                storage_root = await ProjectRepository.get_storage_path(project_uuid)
                usage = await asyncio.to_thread(
                    calculate_storage_usage,
                    ensure_project_storage_root(storage_root),
                    strict=True,
                )
                return JSONResponse(
                    {
                        "success": True,
                        "path": "",
                        "storage_root": storage_root,
                        "quota_mb": project.storage_quota_mb,
                        "usage": usage,
                    }
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get project storage usage: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{project_id}/information/organize-folder")
    async def organize_project_information_from_folder(
        project_id: str,
        payload: ProjectInformationOrganizePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Scan a project filer folder and organize it into project information."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                project_uuid = UUID(project_id)
                user_uuid = UUID(user_info["id"])
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=project_uuid,
                    user_id=user_uuid,
                    permission="write",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                project = await ProjectRepository.get_by_id(session, project_uuid)
                if not project:
                    raise HTTPException(status_code=404, detail="Project not found")

                storage_root = await ProjectRepository.get_storage_path(project_uuid)
                storage_root_path = ensure_project_storage_root(storage_root)

                config = None
                if payload.use_llm:
                    try:
                        from ..config import Config

                        config = Config()
                        config.set("use_tools", False)
                    except Exception as exc:
                        logger.warning(
                            "Project information organizer will use heuristic mode: %s",
                            exc,
                        )

                from ..services.project_information_organizer import (
                    LLMUsageContext,
                    organize_project_folder,
                )

                usage_context = LLMUsageContext(
                    user_id=str(user_uuid),
                    project_id=str(project_uuid),
                    session_id=trusted_request_session_id(
                        request,
                        user_id=user_uuid,
                        project_id=project_uuid,
                    ),
                )
                turn_token = set_turn_context(
                    user_id=usage_context.user_id,
                    project_id=usage_context.project_id,
                    session_id=usage_context.session_id,
                )
                try:
                    result = await organize_project_folder(
                        session,
                        project_id=project_uuid,
                        project_name=project.name,
                        user_id=user_uuid,
                        storage_root=storage_root_path,
                        folder_path=payload.path,
                        apply=payload.apply,
                        use_llm=payload.use_llm,
                        config=config,
                        max_files=max(1, min(200, payload.max_files)),
                        draft_override=payload.draft,
                        usage_context=usage_context,
                    )
                finally:
                    reset_turn_context(turn_token)

                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Failed to organize project information: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{project_id}/information/daily-intake")
    async def run_project_daily_intake(
        project_id: str,
        payload: DailyIntakePayload,
        request: Request,
        _: None = Depends(require_auth_dependency),
    ):
        """Structure a free-form daily note and reflect it into project information."""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")

        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        try:
            session = await db_manager.get_session()
            try:
                project_uuid = UUID(project_id)
                user_uuid = UUID(user_info["id"])
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=project_uuid,
                    user_id=user_uuid,
                    permission="write",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                project = await ProjectRepository.get_by_id(session, project_uuid)
                if not project:
                    raise HTTPException(status_code=404, detail="Project not found")

                config = None
                if payload.use_llm:
                    try:
                        from ..config import Config

                        config = Config()
                        config.set("use_tools", False)
                    except Exception as exc:
                        logger.warning(
                            "Daily intake will use empty draft (no LLM): %s",
                            exc,
                        )

                intake_date = (payload.intake_date or "").strip()
                if not intake_date:
                    from datetime import datetime
                    from zoneinfo import ZoneInfo

                    intake_date = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")

                from ..services.daily_intake_service import run_daily_intake

                from ..services.project_information_organizer import LLMUsageContext

                usage_context = LLMUsageContext(
                    user_id=str(user_uuid),
                    project_id=str(project_uuid),
                    session_id=trusted_request_session_id(
                        request,
                        user_id=user_uuid,
                        project_id=project_uuid,
                    ),
                )
                turn_token = set_turn_context(
                    user_id=usage_context.user_id,
                    project_id=usage_context.project_id,
                    session_id=usage_context.session_id,
                )
                try:
                    result = await run_daily_intake(
                        session,
                        project_id=project_uuid,
                        project_name=project.name,
                        user_id=user_uuid,
                        raw_input=payload.raw_input,
                        intake_date=intake_date,
                        clarification_answers=payload.clarification_answers,
                        apply=payload.apply,
                        use_llm=payload.use_llm,
                        config=config,
                        draft_override=payload.draft,
                        usage_context=usage_context,
                    )
                finally:
                    reset_turn_context(turn_token)

                return JSONResponse(result)
            finally:
                await session.close()
        except HTTPException:
            raise
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Failed to run daily intake: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/resolve/context")
    async def resolve_project_context(
        request: Request,
        project_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        session_id: Optional[str] = None,
        _: None = Depends(require_auth_dependency),
    ):
        """Resolve project context from explicit IDs or workspace links."""
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")

        if not any([project_id, workspace_path, session_id]):
            raise HTTPException(
                status_code=400,
                detail="project_id, workspace_path, or session_id is required",
            )

        try:
            resolver = ProjectContextResolver(get_db_manager())
            context = await resolver.resolve_context(
                project_id=project_id,
                workspace_path=workspace_path,
                session_id=session_id,
                user_id=user_info["id"],
            )
            if not context:
                raise HTTPException(status_code=404, detail="Project not found")

            return JSONResponse({
                "success": True,
                "project": context,
                "match_reason": context.get("match_reason"),
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to resolve project context: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    # ── Storage Context ──────────────────────────────────────────────────
    
    @router.get("/{project_id}/storage-path")
    async def get_project_storage_path(
        project_id: str,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Get the storage path for a project"""
        db_manager = get_db_manager()
        if db_manager is None:
            raise HTTPException(status_code=503, detail="Database not available")
        
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            session = await db_manager.get_session()
            try:
                # Check if user has access
                project_uuid = UUID(project_id)
                project = await ProjectRepository.get_by_id(session, project_uuid)
                if not project:
                    raise HTTPException(status_code=404, detail="Project not found")

                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=project_uuid,
                    user_id=UUID(user_info["id"]),
                    permission="read",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Access denied")

                member = await ProjectRepository.get_member(
                    session,
                    project_id=project_uuid,
                    user_id=UUID(user_info["id"]),
                )
                
                storage_path = await ProjectRepository.get_storage_path(project_uuid)
                metadata = normalize_project_metadata(project.project_metadata)
                is_project_owner = project.owner_id == UUID(user_info["id"])
                is_global_admin = user_info.get("role") == "admin"
                effective_permissions = (
                    member.permissions
                    if member and not (is_project_owner or is_global_admin)
                    else {
                        "read": True,
                        "write": True,
                        "delete": True,
                        "manage_members": True,
                        "manage_settings": True,
                    }
                )
                
                return JSONResponse({
                    "path": storage_path,
                    "project_storage_path": storage_path,
                    "workspace_root": (
                        metadata["links"]["workspace_root"]
                        if is_project_owner or is_global_admin
                        else None
                    ),
                    "wbs_file": metadata["management"].get("wbs_file"),
                    "issue_file": metadata["management"].get("issue_file"),
                    "risk_file": metadata["management"].get("risk_file"),
                    "permissions": effective_permissions,
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get storage path: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    return router
