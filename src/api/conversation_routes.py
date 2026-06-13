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

from ..services.conversation_title_service import ensure_conversation_title

logger = logging.getLogger(__name__)


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

    target_message_id: Optional[str] = None
    branch_index: Optional[int] = None


class UpdateRpSettingsRequest(BaseModel):
    """Request model for updating RP steering settings"""

    creativity: Optional[float] = None
    detail: Optional[float] = None
    tempo: Optional[float] = None
    emotion: Optional[float] = None


def create_conversation_router(
    require_auth, get_current_user, get_llm_for_title_generation=None
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


    async def _get_current_conversation_user(request: Request) -> dict:
        user_info = get_current_user(request)
        if hasattr(user_info, "__await__"):
            user_info = await user_info
        return user_info or {"id": "default_user", "username": "default_user"}

    async def _get_visible_conversation_user_ids(request: Request) -> list[str]:
        user_info = await _get_current_conversation_user(request)
        return [str(user_info.get("id") or "default_user")]

    def _session_is_visible(session, visible_user_ids: list[str]) -> bool:
        if str(session.user_id) in visible_user_ids:
            return True
        for participant in getattr(session, "participants", []) or []:
            if (
                participant.participant_type == "user"
                and str(participant.participant_id) in visible_user_ids
                and participant.status in {"joined", "invited"}
            ):
                return True
        return False

    @router.get("")
    async def list_sessions(
        limit: int = 50,
        offset: int = 0,
        project_id: Optional[str] = None,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Get list of conversation sessions for current user"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            # memory_managerと同じuser_id（default_user）を使用
            repo = ConversationRepository()

            # Handle special 'none' value to filter for conversations without project_id
            filter_project_id = project_id
            if project_id == "none":
                filter_project_id = (
                    ""  # Empty string signals repository to filter for NULL project_id
                )

            sessions = await repo.get_user_sessions(
                visible_user_ids,
                limit=limit,
                offset=offset,
                project_id=filter_project_id,
            )
            total = len(sessions)

            return JSONResponse(
                {
                    "success": True,
                    "conversations": [s.to_dict() for s in sessions],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
            )
        except Exception as e:
            logger.error(f"Failed to list sessions: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/by-project/{project_id}")
    async def get_project_conversations(
        project_id: str,
        limit: int = 50,
        offset: int = 0,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Get conversations for a specific project"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            visible_user_ids = await _get_visible_conversation_user_ids(request)

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
            sessions = [
                session
                for session in await repo.get_sessions_by_project(
                    project_id=project_id, limit=limit, offset=offset
                )
                if _session_is_visible(session, visible_user_ids)
            ]
            total = len(sessions)

            return JSONResponse(
                {
                    "success": True,
                    "conversations": [s.to_dict() for s in sessions],
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "project_id": project_id,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get project conversations: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("")
    async def create_session(
        payload: CreateSessionRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Create a new conversation session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = await _get_current_conversation_user(request)
            user_id = str(user_info.get("id") or "default_user")

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
                project_id=normalized_project_id,
            )
            await repo.ensure_participant(
                str(session.id),
                "user",
                user_id,
                display_name=str(
                    user_info.get("display_name")
                    or user_info.get("username")
                    or "default_user"
                ),
                role="owner",
                status="joined",
            )

            # first_message 挿入
            first_msg_content = None
            try:
                from ..services.character_service import get_character_for_prompt

                char_data = await get_character_for_prompt(payload.character_name)
                if char_data:
                    first_msg_content = char_data.get("first_message", "")
                    if not first_msg_content and char_data.get("alternate_greetings"):
                        import random

                        greetings = char_data["alternate_greetings"]
                        if greetings:
                            first_msg_content = random.choice(greetings)
            except Exception as e:
                logger.warning(f"Failed to get first_message: {e}")

            if first_msg_content:
                await repo.add_message(
                    session_id=str(session.id),
                    role="assistant",
                    content=first_msg_content,
                    sender_type="character",
                    sender_id=payload.character_name,
                    sender_display_name=payload.character_name,
                )

            response = {"success": True, "session": session.to_dict()}
            if first_msg_content:
                response["first_message"] = first_msg_content
            return JSONResponse(response)
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # Note: This route must come before /{session_id} to avoid path matching conflicts
    @router.get("/active/current")
    async def get_active_session(
        character_name: Optional[str] = None,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Get the current active session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            repo = ConversationRepository()
            sessions = await repo.get_user_sessions(
                visible_user_ids, include_inactive=False
            )
            if character_name:
                sessions = [
                    session
                    for session in sessions
                    if session.character_name == character_name
                ]
            session = sessions[0] if sessions else None

            if session:
                messages = await repo.get_session_messages(str(session.id))
                return JSONResponse(
                    {
                        "success": True,
                        "session": session.to_dict(),
                        "messages": [m.to_dict() for m in messages],
                    }
                )
            else:
                return JSONResponse({"success": True, "session": None, "messages": []})
        except Exception as e:
            logger.error(f"Failed to get active session: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{session_id}")
    async def get_session(
        session_id: str, _: None = Depends(require_auth), request: Request = None
    ):
        """Get a specific session with its messages"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id, with_messages=False)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            # Verify ownership
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            return JSONResponse({"success": True, "session": session.to_dict()})
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
        request: Request = None,
    ):
        """Get messages for a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            messages = await repo.get_session_messages(session_id, limit=limit)

            return JSONResponse(
                {"success": True, "messages": [m.to_dict() for m in messages]}
            )
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
        request: Request = None,
    ):
        """Add a message to a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = await _get_current_conversation_user(request)
            user_id = str(user_info.get("id") or "default_user")

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            # Add message
            message = await repo.add_message(
                session_id=session_id,
                role=payload.role,
                content=payload.content,
                sender_type="user" if payload.role == "user" else None,
                sender_id=user_id if payload.role == "user" else None,
                sender_display_name=(
                    str(user_info.get("display_name") or user_info.get("username") or "")
                    if payload.role == "user"
                    else None
                ),
            )

            generated_title = await ensure_conversation_title(
                repo=repo,
                session_id=session_id,
                llm_generator=get_llm_for_title_generation,
            )

            response = {"success": True, "message": message.to_dict()}
            if generated_title:
                response["title"] = generated_title.title
                response["title_source"] = generated_title.source
            return JSONResponse(response)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to add message: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{session_id}/generate-title")
    async def generate_session_title(
        session_id: str,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Generate a title once early conversation context is available."""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            generated = await ensure_conversation_title(
                repo=repo,
                session_id=session_id,
                llm_generator=get_llm_for_title_generation,
            )

            updated = await repo.get_session_by_id(session_id)
            return JSONResponse(
                {
                    "success": True,
                    "title": updated.title if updated else session.title,
                    "generated": generated is not None,
                    "source": generated.source if generated else None,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to generate session title: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.put("/{session_id}")
    async def update_session(
        session_id: str,
        payload: UpdateSessionRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Update session details"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            # Build update dict
            updates = {}
            if payload.title is not None:
                updates["title"] = payload.title
            if payload.is_active is not None:
                updates["is_active"] = payload.is_active
            if payload.project_id is not None:
                # Convert to UUID or None
                from uuid import UUID

                updates["project_id"] = (
                    UUID(payload.project_id) if payload.project_id else None
                )

            if updates:
                await repo.update_session(session_id, **updates)

            # Get updated session
            updated = await repo.get_session_by_id(session_id)

            return JSONResponse(
                {"success": True, "session": updated.to_dict() if updated else None}
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update session: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/{session_id}")
    async def delete_session(
        session_id: str, _: None = Depends(require_auth), request: Request = None
    ):
        """Delete a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            deleted = await repo.delete_session(session_id)

            return JSONResponse({"success": deleted})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete session: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/{session_id}/resume")
    async def resume_session(
        session_id: str, _: None = Depends(require_auth), request: Request = None
    ):
        """Resume (reactivate) a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            # Handle async function
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            # memory_managerと同じuser_id（default_user）を使用
            user_id = "default_user"

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")

            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            # Deactivate all other sessions for this user/character
            all_sessions = await repo.get_user_sessions(visible_user_ids)
            for s in all_sessions:
                if (
                    str(s.id) != session_id
                    and s.character_name == session.character_name
                ):
                    await repo.deactivate_session(str(s.id), touch_activity=False)

            # Activate this session
            await repo.update_session(
                session_id,
                is_active=True,
                touch_activity=False,
            )

            # Get messages for the current active branch
            messages = await repo.get_active_branch_messages(session_id)

            updated = await repo.get_session_by_id(session_id)

            return JSONResponse(
                {
                    "success": True,
                    "session": updated.to_dict() if updated else None,
                    "messages": [m.to_dict() for m in messages],
                }
            )
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
        request: Request = None,
    ):
        """Edit a message by creating a new branch

        This creates a new branch with the edited content,
        deactivating the original message and its descendants.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()

            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session_obj, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            # Verify message belongs to session
            message = await repo.get_message_by_id(message_id)
            if not message:
                raise HTTPException(status_code=404, detail="Message not found")
            if str(message.session_id) != session_id:
                raise HTTPException(
                    status_code=400, detail="Message does not belong to this session"
                )

            # Edit message (creates new branch)
            new_message = await repo.edit_message_and_branch(
                message_id=message_id, new_content=payload.content
            )

            logger.info(
                f"Message {message_id} edited, new branch created: {new_message.id}"
            )

            return JSONResponse(
                {
                    "success": True,
                    "message": new_message.to_dict(),
                    "branch_created": True,
                }
            )
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
        request: Request = None,
    ):
        """Get all sibling branches for a message

        Returns all messages that share the same parent message,
        which represents different branches at that point.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()

            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session_obj, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            # Get sibling branches
            siblings = await repo.get_branch_siblings(message_id)

            # Find current message's index
            current_index = next(
                (i for i, s in enumerate(siblings) if s["id"] == message_id), 0
            )

            return JSONResponse(
                {
                    "success": True,
                    "branches": siblings,
                    "total": len(siblings),
                    "current_index": current_index,
                }
            )
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
        request: Request = None,
    ):
        """Switch to a different branch

        Activates the specified message and its descendants,
        deactivating the current branch.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()

            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session_obj, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            target_message_id = payload.target_message_id
            if not target_message_id and payload.branch_index is not None:
                siblings = await repo.get_branch_siblings(message_id)
                if payload.branch_index < 0 or payload.branch_index >= len(siblings):
                    raise HTTPException(status_code=400, detail="Invalid branch index")
                target_message_id = siblings[payload.branch_index]["id"]
            if not target_message_id:
                raise HTTPException(
                    status_code=400, detail="target_message_id or branch_index is required"
                )

            # Switch branch
            success = await repo.switch_active_branch(
                session_id=session_id, target_message_id=target_message_id
            )

            if not success:
                raise HTTPException(status_code=400, detail="Failed to switch branch")

            # Get updated messages
            messages = await repo.get_active_branch_messages(session_id)

            logger.info(f"Switched to branch with message {target_message_id}")

            return JSONResponse(
                {"success": True, "messages": [m.to_dict() for m in messages]}
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to switch branch: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/{session_id}/active-messages")
    async def get_active_branch_messages(
        session_id: str, _: None = Depends(require_auth), request: Request = None
    ):
        """Get messages in the active branch only

        This filters out inactive branches and returns only
        the messages that should be displayed, with branch info included.
        """
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()

            # Verify session ownership
            session_obj = await repo.get_session_by_id(session_id)
            if not session_obj:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session_obj, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            messages = await repo.get_active_branch_messages(session_id)

            # Add branch info to each message to avoid N+1 queries on frontend
            messages_with_branches = []
            for msg in messages:
                msg_dict = msg.to_dict()

                # Get branch siblings count for this message
                try:
                    siblings = await repo.get_branch_siblings(str(msg.id))
                    msg_dict["branch_count"] = len(siblings)
                    # Find current message's index among siblings
                    msg_dict["branch_index"] = next(
                        (i for i, s in enumerate(siblings) if s["id"] == str(msg.id)), 0
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to get branch info for message {msg.id}: {e}"
                    )
                    msg_dict["branch_count"] = 1
                    msg_dict["branch_index"] = 0

                messages_with_branches.append(msg_dict)

            return JSONResponse({"success": True, "messages": messages_with_branches})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get active branch messages: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ─── RP Steering Settings Endpoints ─────────────────────────────────

    @router.get("/{session_id}/rp-settings")
    async def get_rp_settings(
        session_id: str,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Get RP steering slider settings for a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            rp_settings = session.rp_settings or {
                "creativity": 0.5,
                "detail": 0.5,
                "tempo": 0.5,
                "emotion": 0.5,
            }

            return JSONResponse({"success": True, "rp_settings": rp_settings})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get RP settings: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.put("/{session_id}/rp-settings")
    async def update_rp_settings(
        session_id: str,
        payload: UpdateRpSettingsRequest,
        _: None = Depends(require_auth),
        request: Request = None,
    ):
        """Update RP steering slider settings for a session"""
        if not REPO_AVAILABLE:
            raise HTTPException(status_code=503, detail="Database not available")

        try:
            user_info = get_current_user(request)
            if hasattr(user_info, "__await__"):
                user_info = await user_info
            user_id = "default_user"

            repo = ConversationRepository()
            session = await repo.get_session_by_id(session_id)

            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            visible_user_ids = await _get_visible_conversation_user_ids(request)
            if not _session_is_visible(session, visible_user_ids):
                raise HTTPException(status_code=403, detail="Access denied")

            # 既存設定にマージ
            current_settings = session.rp_settings or {
                "creativity": 0.5,
                "detail": 0.5,
                "tempo": 0.5,
                "emotion": 0.5,
            }
            updates = {k: v for k, v in payload.model_dump().items() if v is not None}
            # 値のバリデーション (0.0 ~ 1.0)
            for key, val in updates.items():
                if not (0.0 <= val <= 1.0):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{key} は 0.0 ~ 1.0 の範囲で指定してください",
                    )
            current_settings.update(updates)

            # DB更新
            from sqlalchemy import update as sa_update
            from ..memory.models import ConversationSession as SessionModel
            from ..memory.database import get_database_manager
            import uuid as uuid_mod

            db_manager = get_database_manager()
            db_session = await db_manager.get_session()
            try:
                stmt = (
                    sa_update(SessionModel)
                    .where(SessionModel.id == uuid_mod.UUID(session_id))
                    .values(rp_settings=current_settings)
                )
                await db_session.execute(stmt)
                await db_session.commit()
            finally:
                await db_session.close()

            return JSONResponse({"success": True, "rp_settings": current_settings})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update RP settings: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
