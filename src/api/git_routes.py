"""
Git API routes for AoiTalk Web Interface

Provides endpoints for local Git version control operations on user/project workspaces.
"""

import logging
from typing import Optional
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# ── Data Models ──────────────────────────────────────────────────────────

class GitCommitPayload(BaseModel):
    """Payload for committing changes"""
    message: str
    storage_context: str  # "user" or "project"
    context_id: Optional[str] = None  # Required for "project" context


class GitStatusPayload(BaseModel):
    """Payload for getting git status"""
    storage_context: str
    context_id: Optional[str] = None


class GitLogPayload(BaseModel):
    """Payload for getting git log"""
    storage_context: str
    context_id: Optional[str] = None
    limit: int = 20


class GitDiffPayload(BaseModel):
    """Payload for getting git diff"""
    storage_context: str
    context_id: Optional[str] = None
    commit_hash: Optional[str] = None


# ── Router Factory ───────────────────────────────────────────────────────

def create_git_router(
    get_user_from_request,
    require_auth_dependency
) -> APIRouter:
    """
    Create the Git router with dependencies injected.
    
    Args:
        get_user_from_request: Function to get current user from request
        require_auth_dependency: Auth dependency for protected routes
        
    Returns:
        APIRouter: Configured router with all Git endpoints
    """
    router = APIRouter(prefix="/api/git", tags=["git"])
    
    # Import git service lazily to avoid circular imports
    from ..services.git_service import (
        GitService,
        GitServiceError,
        get_user_directory,
        get_project_directory,
    )
    
    def _get_repo_path(storage_context: str, context_id: Optional[str], user_id: str) -> Path:
        """Resolve the repository path based on storage context."""
        if storage_context == "user":
            return get_user_directory(user_id)
        elif storage_context == "project":
            if not context_id:
                raise HTTPException(status_code=400, detail="context_id required for project storage")
            return get_project_directory(context_id)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown storage_context: {storage_context}")
    
    @router.get("/available")
    async def check_git_available(_: None = Depends(require_auth_dependency)):
        """Check if git is available on the system"""
        available = GitService.is_git_available()
        return JSONResponse({
            "available": available,
            "message": "Git is available" if available else "Git is not installed or not in PATH"
        })
    
    @router.post("/status")
    async def get_git_status(
        payload: GitStatusPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Get git status for a storage context"""
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            repo_path = _get_repo_path(payload.storage_context, payload.context_id, user_info["id"])
            status = GitService.get_status(repo_path)
            return JSONResponse(status)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get git status: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/commit")
    async def commit_changes(
        payload: GitCommitPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Commit all changes in a storage context"""
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        if not payload.message.strip():
            raise HTTPException(status_code=400, detail="Commit message cannot be empty")
        
        try:
            repo_path = _get_repo_path(payload.storage_context, payload.context_id, user_info["id"])
            
            # Ensure repository exists
            if not GitService.is_repository(repo_path):
                GitService.init_repository(repo_path)
            
            commit_hash = GitService.commit_all(repo_path, payload.message)
            
            if commit_hash:
                return JSONResponse({
                    "success": True,
                    "commit_hash": commit_hash,
                    "message": f"変更をコミットしました: {commit_hash[:8]}"
                })
            else:
                return JSONResponse({
                    "success": False,
                    "message": "コミットする変更がありません"
                })
        except GitServiceError as e:
            logger.error(f"Git commit failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to commit: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/log")
    async def get_git_log(
        payload: GitLogPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Get commit history for a storage context"""
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            repo_path = _get_repo_path(payload.storage_context, payload.context_id, user_info["id"])
            commits = GitService.get_log(repo_path, limit=payload.limit)
            return JSONResponse({
                "commits": commits,
                "total": len(commits)
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get git log: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/diff")
    async def get_git_diff(
        payload: GitDiffPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Get diff for uncommitted changes or a specific commit"""
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            repo_path = _get_repo_path(payload.storage_context, payload.context_id, user_info["id"])
            diff = GitService.get_diff(repo_path, commit_hash=payload.commit_hash)
            return JSONResponse({
                "diff": diff,
                "commit_hash": payload.commit_hash
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get git diff: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/init")
    async def init_repository(
        payload: GitStatusPayload,
        request: Request,
        _: None = Depends(require_auth_dependency)
    ):
        """Initialize a git repository for a storage context"""
        user_info = await get_user_from_request(request)
        if not user_info:
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        try:
            repo_path = _get_repo_path(payload.storage_context, payload.context_id, user_info["id"])
            
            if GitService.is_repository(repo_path):
                return JSONResponse({
                    "success": True,
                    "message": "リポジトリは既に存在します",
                    "already_exists": True
                })
            
            GitService.init_repository(repo_path)
            return JSONResponse({
                "success": True,
                "message": "リポジトリを初期化しました",
                "already_exists": False
            })
        except GitServiceError as e:
            logger.error(f"Failed to init repository: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to init repository: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    return router
