"""
Conversation History API Routes

Provides REST API endpoints for managing conversation sessions and messages.
"""

import logging
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Title generation constants
TITLE_MAX_LENGTH = 50
TITLE_FALLBACK_LENGTH = 40


class CreateSessionRequest(BaseModel):
    """Request model for creating a new session"""
    character_name: str
    project_id: Optional[str] = None


class UpdateSessionRequest(BaseModel):
    """Request model for updating a session"""
    title: Optional[str] = None
    is_active: Optional[bool] = None
    project_id: Optional[str] = None


class AddMessageRequest(BaseModel):
    """Request model for adding a message"""
    role: str  # 'user' or 'assistant'
    content: str


class EditMessageRequest(BaseModel):
    """Request model for editing a message (creates a new branch)"""
    content: str


class SwitchBranchRequest(BaseModel):
    """Request model for switching to a different branch"""
    target_message_id: str


def create_conversation_router(
    require_auth,
    get_current_user,
    get_llm_for_title_generation=None
) -> APIRouter:
    """Create conversation history router
    
    Args:
        require_auth: Auth dependency function
        get_current_user: Function to get current user info from request
        get_llm_for_title_generation: Optional async function to generate title via LLM
        
    Returns:
        APIRouter with conversation endpoints
    """
    router = APIRouter(prefix="/api/conversations", tags=["conversations"])
    
    # Import repository
    try:
        from ..memory.conversation_repository import ConversationRepository
        REPO_AVAILABLE = True
    except ImportError:
        REPO_AVAILABLE = False
        logger.warning("ConversationRepository not available")
    
    async def _generate_title(first_message: str, llm_generator=None) -> str:
        """Generate session title from first message
        
        Args:
            first_message: First user message
            llm_generator: Optional async LLM generation function
            
        Returns:
            Generated title string
        """
        if llm_generator:
            try:
                # Try LLM-based title generation
                prompt = f"""以下のユーザーの最初のメッセージから、この会話のタイトルを生成してください。
タイトルは15文字以内で、内容を簡潔に表すものにしてください。
タイトルのみを出力し、他には何も出力しないでください。

メッセージ: {first_message[:200]}"""
                
                title = await llm_generator(prompt)
                if title:
                    title = title.strip().strip('"\'')
                    if len(title) <= TITLE_MAX_LENGTH:
                        return title
            except Exception as e:
                logger.warning(f"LLM title generation failed: {e}")
        
        # Fallback: use truncated first message
        if len(first_message) > TITLE_FALLBACK_LENGTH:
            return first_message[:TITLE_FALLBACK_LENGTH - 3] + "..."
        return first_message
    
    @router.get("")
    async def list_sessions(
        limit: int = 50,
        offset: int = 0,
        project_id: Optional[str] = None,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Get list of conversation sessions for current user"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"
            
            repo = ConversationRepository()
            
            # Handle special 'none' value to filter for conversations without project_id
            filter_project_id = project_id
            if project_id == "none":
                filter_project_id = ""  # Empty string signals repository to filter for NULL project_id
            
            sessions = await repo.get_user_sessions(
                user_id, 
                limit=limit, 
                offset=offset,
                project_id=filter_project_id
            )
            total = len(sessions)
            
            return JSONResponse({
                "success": True,
                "conversations": [s.to_dict() for s in sessions],
                "total": total,
                "limit": limit,
                "offset": offset
            })
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/by-project/{project_id}")
    async def get_project_conversations(
        project_id: str,
        limit: int = 50,
        offset: int = 0,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Get conversations for a specific project"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            user_id = "default_user"
            
            # Check project access permission
            try:
                from ..memory.project_repository import ProjectRepository
                from ..memory.database import get_database_manager
                from uuid import UUID
                
                db_manager = get_database_manager()
                async with await db_manager.get_session() as db_session:
                    # Get user's UUID (assuming user_info has 'id' field)
                    # For now, we'll skip strict permission check since user_id is "default_user"
                    # In production, you should verify project membership here
                    pass
            except Exception as e:
                logger.warning(f"Could not verify project access: {e}")
            
            repo = ConversationRepository()
            sessions = await repo.get_sessions_by_project(
                project_id=project_id,
                limit=limit,
                offset=offset
            )
            total = len(sessions)
            
            return JSONResponse({
                "success": True,
                "conversations": [s.to_dict() for s in sessions],
                "total": total,
                "limit": limit,
                "offset": offset,
                "project_id": project_id
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get project conversations: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("")
    async def create_session(
        payload: CreateSessionRequest,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Create a new conversation session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"
            
            repo = ConversationRepository()
            
            # Deactivate current active session for this user/character
            active = await repo.get_active_session(user_id, payload.character_name)
            if active:
                await repo.deactivate_session(str(active.id))
            
            # Normalize project_id: convert invalid values to None
            normalized_project_id = payload.project_id
            if payload.project_id:
                # Convert string values like "none", "all", "" to None
                if payload.project_id.lower() in ["none", "all", ""]:
                    normalized_project_id = None
            
            # Create new session
            session = await repo.create_session(
                user_id=user_id,
                character_name=payload.character_name,
                title="",  # Will be generated on first message
                project_id=normalized_project_id
            )
            
            return JSONResponse({
                "success": True,
                "session": session.to_dict()
            })
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    # Note: This route must come before /{session_id} to avoid path matching conflicts
    @router.get("/active/current")
    async def get_active_session(
        character_name: Optional[str] = None,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Get the current active session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"
            
            repo = ConversationRepository()
            session = await repo.get_active_session(user_id, character_name)
            
            if session:
                messages = await repo.get_session_messages(str(session.id))
                return JSONResponse({
                    "success": True,
                    "session": session.to_dict(),
                    "messages": [m.to_dict() for m in messages]
                })
            else:
                return JSONResponse({
                    "success": True,
                    "session": None,
                    "messages": []
                })
        except Exception as e:
            logger.error(f"Failed to get active session: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/{session_id}")
    async def get_session(
        session_id: str,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Get a specific session with its messages"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"
            
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id, with_messages=False)
            
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            
            # Verify ownership
            if session.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
            
            return JSONResponse({
                "success": True,
                "session": session.to_dict()
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get session: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/{session_id}/messages")
    async def get_session_messages(
        session_id: str,
        limit: Optional[int] = None,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Get messages for a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"
            
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)
            
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            
            if session.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
            
            messages = await repo.get_session_messages(session_id, limit=limit)
            
            return JSONResponse({
                "success": True,
                "messages": [m.to_dict() for m in messages]
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get messages: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/{session_id}/messages")
    async def add_message(
        session_id: str,
        payload: AddMessageRequest,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Add a message to a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"
            
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)
            
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            
            if session.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
            
            # Add message
            message = await repo.add_message(
                session_id=session_id,
                role=payload.role,
                content=payload.content
            )
            
            # If this is the first user message and no title yet, generate one
            if payload.role == 'user' and not session.title and session.message_count <= 1:
                title = await _generate_title(
                    payload.content,
                    get_llm_for_title_generation
                )
                await repo.update_session_title(session_id, title)
            
            return JSONResponse({
                "success": True,
                "message": message.to_dict()
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to add message: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.put("/{session_id}")
    async def update_session(
        session_id: str,
        payload: UpdateSessionRequest,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Update session details"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"
            
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)
            
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            
            if session.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
            
            # Build update dict
            updates = {}
            if payload.title is not None:
                updates['title'] = payload.title
            if payload.is_active is not None:
                updates['is_active'] = payload.is_active
            if payload.project_id is not None:
                # Convert to UUID or None
                from uuid import UUID
                updates['project_id'] = UUID(payload.project_id) if payload.project_id else None
            
            if updates:
                await repo.update_session(session_id, **updates)
            
            # Get updated session
            updated = await repo.get_session_by_id(session_id)
            
            return JSONResponse({
                "success": True,
                "session": updated.to_dict() if updated else None
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update session: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.delete("/{session_id}")
    async def delete_session(
        session_id: str,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Delete a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"
            
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)
            
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            
            if session.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
            
            deleted = await repo.delete_session(session_id)
            
            return JSONResponse({
                "success": deleted
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete session: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/{session_id}/resume")
    async def resume_session(
        session_id: str,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Resume (reactivate) a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"
            
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)
            
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            
            if session.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
            
            # Deactivate all other sessions for this user/character
            all_sessions = await repo.get_user_sessions(user_id)
            for s in all_sessions:
                if str(s.id) != session_id and s.character_name == session.character_name:
                    await repo.deactivate_session(str(s.id))
            
            # Activate this session
            await repo.update_session(session_id, is_active=True)
            
            # Get messages for this session
            messages = await repo.get_session_messages(session_id)
            
            updated = await repo.get_session_by_id(session_id)
            
            return JSONResponse({
                "success": True,
                "session": updated.to_dict() if updated else None,
                "messages": [m.to_dict() for m in messages]
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to resume session: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    # ─── Branching Endpoints ─────────────────────────────────────────────
    
    @router.put("/{session_id}/messages/{message_id}")
    async def edit_message(
        session_id: str,
        message_id: str,
        payload: EditMessageRequest,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Edit a message by creating a new branch
        
        This creates a new branch with the edited content,
        deactivating the original message and its descendants.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            user_id = "default_user"
            
            repo = ConversationRepository()
            
            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            if session_obj.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
            
            # Verify message belongs to session
            message = await repo.get_message_by_id(message_id)
            if not message:
                raise HTTPException(status_code=404, detail="Message not found")
            if str(message.session_id) != session_id:
                raise HTTPException(status_code=400, detail="Message does not belong to this session")
            
            # Edit message (creates new branch)
            new_message = await repo.edit_message_and_branch(
                message_id=message_id,
                new_content=payload.content
            )
            
            logger.info(f"Message {message_id} edited, new branch created: {new_message.id}")
            
            return JSONResponse({
                "success": True,
                "message": new_message.to_dict(),
                "branch_created": True
            })
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/{session_id}/messages/{message_id}/branches")
    async def get_message_branches(
        session_id: str,
        message_id: str,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Get all sibling branches for a message
        
        Returns all messages that share the same parent message,
        which represents different branches at that point.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            user_id = "default_user"
            
            repo = ConversationRepository()
            
            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            if session_obj.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
            
            # Get sibling branches
            siblings = await repo.get_branch_siblings(message_id)
            
            # Find current message's index
            current_index = next(
                (i for i, s in enumerate(siblings) if s['id'] == message_id),
                0
            )
            
            return JSONResponse({
                "success": True,
                "branches": siblings,
                "total": len(siblings),
                "current_index": current_index
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get message branches: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/{session_id}/messages/{message_id}/switch-branch")
    async def switch_branch(
        session_id: str,
        message_id: str,
        payload: SwitchBranchRequest,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Switch to a different branch
        
        Activates the specified message and its descendants,
        deactivating the current branch.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            user_id = "default_user"
            
            repo = ConversationRepository()
            
            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            if session_obj.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
            
            # Switch branch
            success = await repo.switch_active_branch(
                session_id=session_id,
                target_message_id=payload.target_message_id
            )
            
            if not success:
                raise HTTPException(status_code=400, detail="Failed to switch branch")
            
            # Get updated messages
            messages = await repo.get_active_branch_messages(session_id)
            
            logger.info(f"Switched to branch with message {payload.target_message_id}")
            
            return JSONResponse({
                "success": True,
                "messages": [m.to_dict() for m in messages]
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to switch branch: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.get("/{session_id}/active-messages")
    async def get_active_branch_messages(
        session_id: str,
        _: None = Depends(require_auth),
        request: Request = None
    ):
        """Get messages in the active branch only
        
        This filters out inactive branches and returns only
        the messages that should be displayed, with branch info included.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")
        
        try:
            user_info = get_current_user(request)
            if hasattr(user_info, '__await__'):
                user_info = await user_info
            user_id = "default_user"
            
            repo = ConversationRepository()
            
            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            if session_obj.user_id != user_id:
                raise HTTPException(status_code=403, detail="Access denied")
            
            messages = await repo.get_active_branch_messages(session_id)
            
            # Add branch info to each message to avoid N+1 queries on frontend
            messages_with_branches = []
            for msg in messages:
                msg_dict = msg.to_dict()
                
                # Get branch siblings count for this message
                try:
                    siblings = await repo.get_branch_siblings(str(msg.id))
                    msg_dict['branch_count'] = len(siblings)
                    # Find current message's index among siblings
                    msg_dict['branch_index'] = next(
                        (i for i, s in enumerate(siblings) if s['id'] == str(msg.id)),
                        0
                    )
                except Exception as e:
                    logger.warning(f"Failed to get branch info for message {msg.id}: {e}")
                    msg_dict['branch_count'] = 1
                    msg_dict['branch_index'] = 0
                
                messages_with_branches.append(msg_dict)
            
            return JSONResponse({
                "success": True,
                "messages": messages_with_branches
            })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get active branch messages: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    return router
