"""
Project API routes for AoiTalk Web Interface

Provides endpoints for project management, member management, and join requests.
"""

import logging
from pathlib import PurePosixPath
from typing import Optional, List, Any
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request, Depends, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ── Data Models ──────────────────────────────────────────────────────────


class CreateProjectPayload(BaseModel):
    """Payload for creating a new project"""
    name: str
    description: Optional[str] = None
    slug: Optional[str] = None
    aliases: Optional[List[str]] = None
    allow_join_requests: bool = True
    storage_quota_mb: int = 1000
    project_metadata: Optional[dict[str, Any]] = None


class UpdateProjectPayload(BaseModel):
    """Payload for updating project settings"""
    name: Optional[str] = None
    description: Optional[str] = None
    aliases: Optional[List[str]] = None
    allow_join_requests: Optional[bool] = None
    storage_quota_mb: Optional[int] = None
    project_metadata: Optional[dict[str, Any]] = None


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


class ProjectInformationOrganizePayload(BaseModel):
    """Payload for organizing project filer documents into project information."""
    path: str = ""
    apply: bool = False
    use_llm: bool = True
    max_files: int = 80
    draft: Optional[dict[str, Any]] = None


# ── Router Factory ───────────────────────────────────────────────────────


def create_project_router(
    get_db_manager,
    get_user_from_request,
    require_auth_dependency
) -> APIRouter:
    """
    Create the project router with dependencies injected.
    
    Args:
        get_db_manager: Function to get database manager instance
        get_user_from_request: Function to get current user from request
        require_auth_dependency: Auth dependency for protected routes
        
    Returns:
        APIRouter: Configured router with all project endpoints
    """
    router = APIRouter(prefix="/api/projects", tags=["projects"])
    
    # Import repository lazily to avoid circular imports
    from ..memory.project_repository import ProjectRepository
    from ..memory.user_repository import UserRepository
    from ..services.project_context import (
        ProjectContextResolver,
        merge_project_metadata,
        normalize_project_metadata,
    )
    from ..tools.file_explorer import (
        get_root_dir as get_workspace_root,
        list_directory as list_workspace_directory,
        create_directory as create_workspace_directory,
        upload_file as upload_workspace_file,
        download_file as download_workspace_file,
        rename_item as rename_workspace_item,
        delete_item as delete_workspace_item,
        get_file_info as get_workspace_file_info,
        get_preview as get_workspace_file_preview,
    )
    from ..tools.file_explorer.storage_context import calculate_storage_usage

    def normalize_project_member_path(storage_root: str, path: Optional[str]) -> str:
        clean = (path or "").replace("\\", "/").strip("/")
        if not clean:
            return storage_root

        parts = PurePosixPath(clean).parts
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
        target = get_workspace_root() / storage_root
        target.mkdir(parents=True, exist_ok=True)
        return target
    
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
                project = await ProjectRepository.create_project(
                    session,
                    owner_id=UUID(user_info["id"]),
                    name=payload.name,
                    description=payload.description,
                    slug=payload.slug,
                    aliases=payload.aliases,
                    allow_join_requests=payload.allow_join_requests,
                    storage_quota_mb=payload.storage_quota_mb,
                    project_metadata=normalize_project_metadata(payload.project_metadata)
                    if payload.project_metadata is not None
                    else None,
                )
                
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
                
                # Check if user has access (must be member)
                member = await ProjectRepository.get_member(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"])
                )
                
                if not member:
                    raise HTTPException(status_code=403, detail="Access denied")
                
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
                # Check if user is a member
                member = await ProjectRepository.get_member(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"])
                )
                
                if not member:
                    raise HTTPException(status_code=403, detail="Access denied")
                
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
                
                member = await ProjectRepository.update_member(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_id),
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
                
                member = await ProjectRepository.approve_join_request(
                    session,
                    request_id=UUID(request_id),
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
                result = list_workspace_directory(normalize_project_member_path(storage_root, path))
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
                result = get_workspace_file_info(normalize_project_member_path(storage_root, path))
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
                result = get_workspace_file_preview(normalize_project_member_path(storage_root, path))
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
                result = create_workspace_directory(normalize_project_member_path(storage_root, payload.path), payload.name)
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

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                file_bytes = await file.read()
                result = upload_workspace_file(
                    normalize_project_member_path(storage_root, path),
                    file.filename or "unnamed_file",
                    file_bytes,
                )
                if not result.get("success"):
                    raise HTTPException(status_code=400, detail=result.get("error", "Failed to upload file"))
                if result.get("path"):
                    result["path"] = strip_project_storage_prefix(storage_root, result.get("path")) or ""
                return JSONResponse(result)
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
                result = rename_workspace_item(normalize_project_member_path(storage_root, payload.path), payload.new_name)
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
                has_perm = await ProjectRepository.has_permission(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"]),
                    permission="delete",
                )
                if not has_perm:
                    raise HTTPException(status_code=403, detail="Permission denied")

                storage_root = await ProjectRepository.get_storage_path(UUID(project_id))
                ensure_project_storage_root(storage_root)
                result = delete_workspace_item(normalize_project_member_path(storage_root, path))
                if not result.get("success"):
                    raise HTTPException(status_code=400, detail=result.get("error", "Failed to delete file"))
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
                content, filename, mime_type = download_workspace_file(normalize_project_member_path(storage_root, path))
                if content is None:
                    raise HTTPException(status_code=404, detail="File not found")

                from fastapi.responses import Response
                from urllib.parse import quote

                ascii_filename = filename.encode("ascii", "replace").decode("ascii")
                encoded_filename = quote(filename, safe="")
                content_disposition = f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded_filename}"

                return Response(
                    content=content,
                    media_type=mime_type,
                    headers={"Content-Disposition": content_disposition},
                )
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to download project file: {e}")
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
                member = await ProjectRepository.get_member(
                    session,
                    project_id=project_uuid,
                    user_id=UUID(user_info["id"]),
                )
                if not member:
                    raise HTTPException(status_code=403, detail="Access denied")

                project = await ProjectRepository.get_by_id(session, project_uuid)
                if not project:
                    raise HTTPException(status_code=404, detail="Project not found")

                storage_root = await ProjectRepository.get_storage_path(project_uuid)
                usage = calculate_storage_usage(ensure_project_storage_root(storage_root))
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
                    organize_project_folder,
                )

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
                )
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

                member = await ProjectRepository.get_member(
                    session,
                    project_id=project_uuid,
                    user_id=UUID(user_info["id"])
                )
                
                if not member:
                    raise HTTPException(status_code=403, detail="Access denied")
                
                storage_path = await ProjectRepository.get_storage_path(project_uuid)
                metadata = normalize_project_metadata(project.project_metadata)
                
                return JSONResponse({
                    "path": storage_path,
                    "project_storage_path": storage_path,
                    "workspace_root": metadata["links"]["workspace_root"],
                    "wbs_file": metadata["management"].get("wbs_file"),
                    "issue_file": metadata["management"].get("issue_file"),
                    "risk_file": metadata["management"].get("risk_file"),
                    "permissions": member.permissions
                })
            finally:
                await session.close()
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get storage path: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    return router
