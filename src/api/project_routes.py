"""
Project API routes for AoiTalk Web Interface

Provides endpoints for project management, member management, and join requests.
"""

import logging
from typing import Optional, List
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ── Data Models ──────────────────────────────────────────────────────────


class CreateProjectPayload(BaseModel):
    """Payload for creating a new project"""
    name: str
    description: Optional[str] = None
    slug: Optional[str] = None
    allow_join_requests: bool = True
    storage_quota_mb: int = 1000


class UpdateProjectPayload(BaseModel):
    """Payload for updating project settings"""
    name: Optional[str] = None
    description: Optional[str] = None
    allow_join_requests: Optional[bool] = None
    storage_quota_mb: Optional[int] = None


class AddMemberPayload(BaseModel):
    """Payload for adding a member to project"""
    user_id: str
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
                    allow_join_requests=payload.allow_join_requests,
                    storage_quota_mb=payload.storage_quota_mb
                )
                
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
                projects = await ProjectRepository.get_user_projects(
                    session,
                    user_id=UUID(user_info["id"])
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
                
                if str(project.owner_id) != user_info["id"]:
                    raise HTTPException(status_code=403, detail="Only owner can delete project")
                
                deleted = await ProjectRepository.delete_project(session, UUID(project_id))
                
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
                
                member = await ProjectRepository.add_member(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(payload.user_id),
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
                member = await ProjectRepository.get_member(
                    session,
                    project_id=UUID(project_id),
                    user_id=UUID(user_info["id"])
                )
                
                if not member:
                    raise HTTPException(status_code=403, detail="Access denied")
                
                storage_path = await ProjectRepository.get_storage_path(UUID(project_id))
                
                return JSONResponse({
                    "path": storage_path,
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
