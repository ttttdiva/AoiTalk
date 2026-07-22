"""会話メッセージの非同期ディスパッチ・生成制御ルート (server.py から移設)"""

import logging
import time
from typing import TYPE_CHECKING, Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ...assistant.chat_attachment_utils import sanitize_chat_attachments
from ...llm.generation_policy import resolve_generation_profile
from ...llm.tool_policy import (
    command_capabilities_for_current_turn_text,
    sanitize_command_capabilities,
)
from ...memory.conversation_repository import ConversationRepository
from ...services.agent_run_service import AgentRunService
from ..router_helpers import cookie_auth_dependency
from .payloads import (
    ConversationDispatchRequest,
    UserMessage,
    effective_include_project_context,
    sanitize_response_model_selection,
)

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


async def inherit_command_capabilities_from_edit_source(
    *,
    session_id: str,
    edit_message_id: str | None,
    command_capabilities: Any,
) -> tuple[str, ...]:
    """Preserve slash-command capabilities when rerunning/editing a command turn."""
    sanitized = sanitize_command_capabilities(command_capabilities)
    if sanitized or not edit_message_id:
        return sanitized

    repository = ConversationRepository()
    try:
        source_message = await repository.get_message_by_id(edit_message_id)
    except Exception:
        logger.exception(
            "Failed to load source message for command capability inheritance: %s",
            edit_message_id,
        )
        return sanitized

    if (
        source_message is None
        or str(source_message.session_id) != str(session_id)
        or source_message.role != "user"
    ):
        return sanitized

    metadata = source_message.message_metadata
    if isinstance(metadata, dict):
        inherited = sanitize_command_capabilities(
            metadata.get("command_capabilities")
        )
        if inherited:
            return inherited

    try:
        sibling_messages = await repository.get_branch_siblings(edit_message_id)
    except Exception:
        logger.exception(
            "Failed to load branch siblings for command capability inheritance: %s",
            edit_message_id,
        )
        return sanitized

    source_content = str(getattr(source_message, "content", "") or "").strip()
    for sibling in sibling_messages:
        if not isinstance(sibling, dict):
            continue
        if str(sibling.get("id") or "") == str(edit_message_id):
            continue
        if sibling.get("role") != "user":
            continue
        if str(sibling.get("content") or "").strip() != source_content:
            continue
        inherited = sanitize_command_capabilities(
            (sibling.get("metadata") or {}).get("command_capabilities")
        )
        if inherited:
            return inherited
    return sanitized


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
        command_capabilities = await inherit_command_capabilities_from_edit_source(
            session_id=session_id,
            edit_message_id=payload.edit_message_id,
            command_capabilities=payload.command_capabilities,
        )
        command_capabilities = command_capabilities_for_current_turn_text(
            message,
            command_capabilities,
        )
        if "docs_ingest" in command_capabilities:
            lines = message.strip().splitlines()
            clip_body = (
                "\n".join(lines[1:]).strip()
                if lines and lines[0].strip().casefold() == "/clip"
                else message.strip()
            )
            if not clip_body:
                raise HTTPException(
                    status_code=400,
                    detail="取り込む情報を入力してください",
                )
        if "work_intake" in command_capabilities:
            lines = message.strip().splitlines()
            inbox_body = (
                "\n".join(lines[1:]).strip()
                if lines and lines[0].strip().casefold() == "/inbox"
                else message.strip()
            )
            mail_attachments = [
                item
                for item in (payload.attachments or [])
                if str(item.get("name") or "").casefold().endswith((".msg", ".eml"))
            ]
            if not inbox_body and not mail_attachments:
                raise HTTPException(
                    status_code=400,
                    detail="処理するテキストまたはメールを入力してください",
                )
        include_project_context = effective_include_project_context(
            message=message,
            requested=payload.include_project_context,
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
                    metadata["attachments"] = sanitize_chat_attachments(
                        payload.attachments,
                        include_binary=False,
                    )
                if command_capabilities:
                    metadata["command_capabilities"] = list(command_capabilities)

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
        try:
            agent_run = await AgentRunService().create_run(
                session_id=session_id,
                user_id=user_id,
                project_id=payload.project_id,
                trigger_message_id=persisted_user_message_id,
                objective=message,
                run_type="chat_turn",
                generation_profile=generation_profile,
                metadata={
                    "client_message_id": payload.client_message_id,
                    "include_project_context": include_project_context,
                    "requested_include_project_context": (
                        payload.include_project_context
                    ),
                    "command_capabilities": list(command_capabilities),
                    "tools_required": payload.tools_required,
                    "edit_message_id": payload.edit_message_id,
                    "response_model": response_model,
                    "attachment_count": len(payload.attachments or []),
                    "dispatch_source": "conversation_dispatch",
                },
            )
            agent_run_id = str(agent_run["id"])
        except Exception as e:
            logger.exception("Failed to create agent run for dispatch")
            raise HTTPException(
                status_code=500,
                detail="Failed to create agent run",
            ) from e

        server._queue_user_message(
            {
                "message": message,
                "session_id": session_id,
                "agent_run_id": agent_run_id,
                "_sender_user_id": user_id,
                "_sender_display_name": str(
                    (user_info or {}).get("display_name")
                    or (user_info or {}).get("username")
                    or user_id
                ),
                "project_id": payload.project_id,
                "generation_profile": generation_profile,
                "include_project_context": include_project_context,
                "edit_message_id": payload.edit_message_id,
                "response_model": response_model,
                "client_message_id": payload.client_message_id,
                "command_capabilities": list(command_capabilities),
                "tools_required": payload.tools_required,
                "skip_user_persistence": skip_user_persistence,
                "persisted_user_message_id": persisted_user_message_id,
                "attachments": payload.attachments or [],
                "attachment_context": payload.attachment_context,
                "_response_started_at_monotonic": time.monotonic(),
            }
        )

        return JSONResponse(
            {
                "success": True,
                "queued": True,
                "session_id": session_id,
                "user_message_id": persisted_user_message_id,
                "agent_run_id": agent_run_id,
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

    @app.get("/api/conversations/{session_id}/generation/status")
    async def get_conversation_generation_status(
        session_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Return the currently running generation status for a conversation."""
        user_info = await server._get_user_info_from_request(request)
        user_id = str((user_info or {}).get("id") or "default_user")
        if not await server._websocket_session_allowed(session_id, user_id):
            raise HTTPException(status_code=403, detail="Access denied")
        return JSONResponse(
            {"success": True, **server.get_conversation_generation_status(session_id)}
        )

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
