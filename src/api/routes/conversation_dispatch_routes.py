"""会話メッセージの非同期ディスパッチ・生成制御ルート (server.py から移設)"""

import logging
from typing import TYPE_CHECKING, Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ...llm.generation_policy import resolve_generation_profile
from ..router_helpers import cookie_auth_dependency
from .payloads import (
    ConversationDispatchRequest,
    UserMessage,
    sanitize_response_model_selection,
)

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_conversation_dispatch_routes(app: FastAPI, server: "WebChatServer") -> None:
    """dispatch / generation stop / generation steer ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    @app.post("/api/conversations/{session_id}/dispatch")
    async def dispatch_conversation_message(
        session_id: str,
        payload: ConversationDispatchRequest,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Queue a user message for async conversation processing."""
        message = (payload.message or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        if not server.on_user_input:
            raise HTTPException(
                status_code=503,
                detail="Conversation generation is not ready",
            )
        try:
            generation_profile = resolve_generation_profile(
                payload.generation_profile
            ).value
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        user_info = await server._get_user_info_from_request(request)
        user_id = str((user_info or {}).get("id") or "default_user")
        if not await server._websocket_session_allowed(session_id, user_id):
            raise HTTPException(status_code=403, detail="Access denied")
        await server._attach_project_to_conversation_if_missing(
            session_id, payload.project_id
        )

        skip_user_persistence = bool(
            payload.skip_user_persistence and payload.persisted_user_message_id
        )
        persisted_user_message_id = payload.persisted_user_message_id
        if skip_user_persistence:
            try:
                from ...memory.conversation_repository import ConversationRepository

                existing_message = await ConversationRepository().get_message_by_id(
                    persisted_user_message_id
                )
                if (
                    existing_message is None
                    or str(existing_message.session_id) != session_id
                    or existing_message.role != "user"
                ):
                    raise ValueError("persisted user message does not match session")
            except Exception as e:
                logger.warning("Invalid persisted user message for dispatch: %s", e)
                raise HTTPException(
                    status_code=400,
                    detail="Invalid persisted user message",
                ) from e
        else:
            try:
                from ...assistant.chat_turn_persistence import ChatTurnPersistence

                metadata: Dict[str, Any] = {}
                if payload.client_message_id:
                    metadata["client_message_id"] = payload.client_message_id
                if payload.attachments:
                    metadata["attachments"] = payload.attachments

                persisted_user_message = await ChatTurnPersistence().save_user_message(
                    session_id=session_id,
                    content=message,
                    metadata=metadata,
                    branch_from_message_id=payload.edit_message_id,
                    sender_type="user",
                    sender_id=user_id,
                    sender_display_name=str(
                        (user_info or {}).get("display_name")
                        or (user_info or {}).get("username")
                        or user_id
                    ),
                )
                if persisted_user_message is None:
                    raise RuntimeError("user message was not persisted")
                persisted_user_message_id = str(persisted_user_message.id)
                skip_user_persistence = True
            except Exception as e:
                logger.exception("Failed to persist dispatch user message")
                raise HTTPException(
                    status_code=500,
                    detail="Failed to persist user message",
                ) from e

        response_model = sanitize_response_model_selection(payload.response_model)
        server._queue_user_message(
            {
                "message": message,
                "session_id": session_id,
                "_sender_user_id": user_id,
                "_sender_display_name": str(
                    (user_info or {}).get("display_name")
                    or (user_info or {}).get("username")
                    or user_id
                ),
                "project_id": payload.project_id,
                "generation_profile": generation_profile,
                "include_project_context": payload.include_project_context,
                "edit_message_id": payload.edit_message_id,
                "response_model": response_model,
                "client_message_id": payload.client_message_id,
                "skip_user_persistence": skip_user_persistence,
                "persisted_user_message_id": persisted_user_message_id,
                "attachments": payload.attachments or [],
                "attachment_context": payload.attachment_context,
            }
        )

        return JSONResponse(
            {
                "success": True,
                "queued": True,
                "session_id": session_id,
                "user_message_id": persisted_user_message_id,
            },
            status_code=202,
        )

    @app.post("/api/conversations/{session_id}/generation/stop")
    async def stop_conversation_generation(
        session_id: str,
        _: None = Depends(require_auth),
    ):
        """Stop the active assistant generation for a conversation session."""
        result = await server._handle_stop_generation({"session_id": session_id})
        return JSONResponse({"success": True, **result})

    @app.post("/api/conversations/{session_id}/generation/steer")
    async def steer_conversation_generation(
        session_id: str,
        payload: UserMessage,
        _: None = Depends(require_auth),
    ):
        """Queue an extra instruction for the active generation."""
        result = await server._handle_steer_generation(
            {"session_id": session_id, "message": payload.message}
        )
        return JSONResponse({"success": True, **result})
