"""会話メッセージの非同期ディスパッチ・生成制御ルート (server.py から移設)"""

import base64
import logging
import time
from typing import TYPE_CHECKING, Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ...assistant.chat_attachment_utils import sanitize_chat_attachments
from ...llm.generation_policy import resolve_generation_profile
from ...llm.planning_policy import resolve_planning_policy
from ...llm.tool_policy import (
    command_capabilities_for_current_turn_text,
    filter_review_command_capabilities,
    sanitize_command_capabilities,
)
from ...memory.conversation_repository import ConversationRepository
from ...services.agent_run_service import (
    AgentRunService,
    DispatchConflictError,
    conversation_dispatch_fingerprint,
)
from ...services.mention_resolver import normalize_mentions
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


def _payload_field_was_provided(payload: Any, field_name: str) -> bool:
    """Support Pydantic v2 and v1 while preserving explicit nulls."""
    fields_set = getattr(payload, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(payload, "__fields_set__", set())
    return field_name in fields_set


def _server_trusted_legacy_marker(server: Any) -> object | None:
    """Return the process-local legacy marker only for an auth-disabled server."""
    if getattr(server, "auth_enabled", True) is not False:
        return None
    from ..server_parts.conversation_mixin import TRUSTED_LEGACY_MARKER

    return TRUSTED_LEGACY_MARKER


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
        mentions = normalize_mentions(payload.mentions)
        # Idempotency follows the canonical structured target, not a mutable
        # client display label.  The full normalized payload is still retained
        # in the durable outbox for the common WebSocket resolver.
        mention_fingerprint = [
            {"type": item["type"], "id": item["id"]}
            for item in mentions
        ]
        if not message and not payload.attachments and not payload.mentions:
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
            planning_policy = resolve_planning_policy(
                getattr(payload, "planning_policy", None)
            ).value
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        user_info = await server._get_user_info_from_request(request)
        user_id = str((user_info or {}).get("id") or "default_user")
        trusted_legacy_marker = _server_trusted_legacy_marker(server)
        if not await server._websocket_session_allowed(
            session_id,
            user_id,
            require_write=True,
            is_admin=bool(user_info and user_info.get("role") == "admin"),
        ):
            raise HTTPException(status_code=403, detail="Access denied")
        command_capabilities = await inherit_command_capabilities_from_edit_source(
            session_id=session_id,
            edit_message_id=payload.edit_message_id,
            command_capabilities=payload.command_capabilities,
        )
        command_capabilities = command_capabilities_for_current_turn_text(
            message,
            command_capabilities,
        )
        if generation_profile == "review":
            command_capabilities = filter_review_command_capabilities(
                command_capabilities
            )
        if "work_intake" in command_capabilities:
            lines = message.strip().splitlines()
            inbox_body = (
                "\n".join(lines[1:]).strip()
                if lines and lines[0].strip().casefold() == "/inbox"
                else message.strip()
            )
            attachments = list(payload.attachments or [])
            failed_attachments = [
                item for item in attachments if item.get("upload_failed")
            ]
            if failed_attachments:
                raise HTTPException(
                    status_code=400,
                    detail="アップロードに失敗した添付ファイルがあります",
                )

            def has_work_intake_source(item: Dict[str, Any]) -> bool:
                name = str(item.get("name") or "").strip()
                stored_path = str(
                    item.get("project_relative_path") or item.get("path") or ""
                ).strip()
                data_url = str(item.get("data_url") or "").strip()
                is_mail = name.casefold().endswith((".msg", ".eml"))
                valid_mail_data = False
                if is_mail and data_url:
                    header, separator, encoded = data_url.partition(",")
                    if (
                        separator
                        and header.casefold().startswith("data:")
                        and header.casefold().endswith(";base64")
                        and encoded
                        and len(encoded) <= 35 * 1024 * 1024
                    ):
                        try:
                            decoded = base64.b64decode(encoded, validate=True)
                            valid_mail_data = bool(decoded) and len(decoded) <= 25 * 1024 * 1024
                        except (ValueError, TypeError):
                            valid_mail_data = False
                return bool(
                    name
                    and (
                        stored_path
                        or valid_mail_data
                    )
                )

            usable_attachments = [
                item for item in attachments if has_work_intake_source(item)
            ]
            if len(usable_attachments) != len(attachments):
                raise HTTPException(
                    status_code=400,
                    detail="保存先を確認できない添付ファイルがあります",
                )
            if not inbox_body and not usable_attachments:
                raise HTTPException(
                    status_code=400,
                    detail="処理するテキストまたは添付ファイルを入力してください",
                )
        elif not message and not mentions:
            raise HTTPException(status_code=400, detail="message is required")
        try:
            effective_attached_project_id = (
                await server._attach_project_to_conversation_if_missing(
                    session_id,
                    payload.project_id,
                    user_id=str((user_info or {}).get("id") or "") or None,
                    user_role=(user_info or {}).get("role"),
                    authenticated=(getattr(server, "auth_enabled", None) is True),
                    trusted_legacy=trusted_legacy_marker is not None,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        conversation = await ConversationRepository().get_session_by_id(
            session_id, with_messages=False
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Session not found")
        generation_status = server.get_conversation_generation_status(session_id)
        if generation_status.get("running") and generation_status.get("status") == "cancellation_pending":
            raise HTTPException(
                status_code=409,
                detail="前の応答の停止処理が完了するまで新しい応答を開始できません",
            )
        stored_app_id = str(conversation.app_id) if conversation.app_id else None
        stored_app_target_id = (
            str(conversation.app_target_id) if conversation.app_target_id else None
        )
        app_scope_provided = _payload_field_was_provided(payload, "app_id") or _payload_field_was_provided(payload, "app_target_id")
        app_id_provided = _payload_field_was_provided(payload, "app_id")
        app_target_id_provided = _payload_field_was_provided(payload, "app_target_id")
        effective_project_id = effective_attached_project_id or (
            str(conversation.project_id) if conversation.project_id else None
        )
        effective_app_id = (
            str(payload.app_id) if payload.app_id else None
            if app_id_provided
            else stored_app_id
        )
        effective_app_target_id = (
            str(payload.app_target_id) if payload.app_target_id else None
            if app_target_id_provided
            else stored_app_target_id
        )
        if effective_app_id:
            from uuid import UUID

            from sqlalchemy import and_, select

            from ...memory.database import get_database_manager
            from ...memory.models import App, AppTarget, ProjectApp
            from ...services.app_service import AppAccessError, AppService

            try:
                app_uuid = UUID(effective_app_id)
                target_uuid = UUID(effective_app_target_id) if effective_app_target_id else None
                user_uuid = UUID(user_id)
                project_uuid = UUID(effective_project_id) if effective_project_id else None
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="App context UUIDが不正です") from exc
            db_session = await get_database_manager().get_session()
            try:
                app = await db_session.scalar(select(App).where(App.id == app_uuid).limit(1))
                if not app:
                    raise HTTPException(status_code=404, detail="App not found")
                try:
                    await AppService().require_permission(
                        db_session,
                        app,
                        user_id=user_uuid,
                        required="viewer",
                        user_role=(user_info or {}).get("role"),
                    project_id=project_uuid,
                    )
                except AppAccessError as exc:
                    raise HTTPException(status_code=403, detail="Appを閲覧できません") from exc
                if project_uuid is not None:
                    binding = await db_session.scalar(select(ProjectApp).where(
                        ProjectApp.project_id == project_uuid,
                        ProjectApp.app_id == app_uuid,
                    ).limit(1))
                    if binding is None or not binding.enabled:
                        raise HTTPException(status_code=403, detail="このProjectではAppが有効化されていません")
                if target_uuid:
                    target = await db_session.scalar(select(AppTarget).where(
                        and_(AppTarget.id == target_uuid, AppTarget.app_id == app_uuid)
                    ).limit(1))
                    if not target:
                        raise HTTPException(status_code=404, detail="App Target not found")
            finally:
                await db_session.close()
            if app_scope_provided:
                await ConversationRepository().update_session(
                    session_id,
                    touch_activity=False,
                    app_id=app_uuid,
                    app_target_id=target_uuid,
                )
        elif app_scope_provided:
            await ConversationRepository().update_session(
                session_id,
                touch_activity=False,
                app_id=None,
                app_target_id=None,
            )
        include_project_context = effective_include_project_context(
            message=message,
            requested=payload.include_project_context,
            app_context_selected=bool(effective_app_id),
            attachment_present=bool(effective_project_id and payload.attachments),
            project_selected=bool(effective_project_id),
        )
        response_model = sanitize_response_model_selection(payload.response_model)
        run_metadata = {
            "client_message_id": payload.client_message_id,
            "planning_policy": planning_policy,
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
            "app_id": effective_app_id,
            "app_target_id": effective_app_target_id,
            "mention_count": len(mentions),
        }
        agent_run_service = AgentRunService()
        sender_display_name = str(
            (user_info or {}).get("display_name")
            or (user_info or {}).get("username")
            or user_id
        )
        message_metadata: Dict[str, Any] = {}
        if payload.client_message_id:
            message_metadata["client_message_id"] = payload.client_message_id
        if payload.attachments:
            message_metadata["attachments"] = sanitize_chat_attachments(
                payload.attachments,
                include_binary=False,
            )
        if command_capabilities:
            message_metadata["command_capabilities"] = list(command_capabilities)
        if mentions:
            message_metadata["mentions"] = mentions

        skip_user_persistence = bool(
            payload.skip_user_persistence and payload.persisted_user_message_id
        )
        persisted_user_message_id = payload.persisted_user_message_id
        if skip_user_persistence and not payload.client_message_id:
            try:
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

        if payload.client_message_id:
            durable_payload = {
                "message": message,
                "session_id": session_id,
                "_sender_user_id": user_id,
                "_sender_is_admin": bool(
                    not getattr(server, "auth_enabled", True)
                    or (user_info or {}).get("role") == "admin"
                ),
                "_sender_display_name": sender_display_name,
                "project_id": effective_project_id,
                "app_id": effective_app_id,
                "app_target_id": effective_app_target_id,
                "generation_profile": generation_profile,
                "planning_policy": planning_policy,
                "include_project_context": include_project_context,
                "edit_message_id": payload.edit_message_id,
                "response_model": response_model,
                "client_message_id": payload.client_message_id,
                "command_capabilities": list(command_capabilities),
                "tools_required": payload.tools_required,
                "attachments": payload.attachments or [],
                "attachment_context": payload.attachment_context,
                "mentions": mentions,
            }
            request_fingerprint = conversation_dispatch_fingerprint(
                {
                    "message": message,
                    "project_id": effective_project_id,
                    "app_id": effective_app_id,
                    "app_target_id": effective_app_target_id,
                    "generation_profile": generation_profile,
                    "planning_policy": planning_policy,
                    "include_project_context": include_project_context,
                    "edit_message_id": payload.edit_message_id,
                    "response_model": response_model,
                    "command_capabilities": list(command_capabilities),
                    "tools_required": payload.tools_required,
                    "persisted_user_message_id": (
                        payload.persisted_user_message_id
                        if skip_user_persistence
                        else None
                    ),
                    "attachments": payload.attachments or [],
                    "attachment_context": payload.attachment_context,
                    "mentions": mention_fingerprint,
                }
            )
            try:
                agent_run, persisted_user_message_id, _created = (
                    await agent_run_service.create_or_get_dispatch_turn(
                        session_id=session_id,
                        client_message_id=payload.client_message_id,
                        content=message,
                        message_metadata=message_metadata,
                        sender_type="user",
                        sender_id=user_id,
                        sender_display_name=sender_display_name,
                        edit_message_id=payload.edit_message_id,
                        outbox_payload=durable_payload,
                        request_fingerprint=request_fingerprint,
                        persisted_user_message_id=(
                            payload.persisted_user_message_id
                            if skip_user_persistence
                            else None
                        ),
                        user_id=user_id,
                        project_id=effective_project_id,
                        app_id=effective_app_id,
                        app_target_id=effective_app_target_id,
                        objective=message,
                        generation_profile=generation_profile,
                        metadata=run_metadata,
                    )
                )
            except DispatchConflictError as e:
                raise HTTPException(
                    status_code=409,
                    detail=str(e),
                ) from e
            except Exception as e:
                logger.exception("Failed to persist atomic conversation dispatch")
                raise HTTPException(
                    status_code=500,
                    detail="Failed to persist conversation dispatch",
                ) from e

            agent_run_id = str(agent_run["id"])
            try:
                delivery = await agent_run_service.claim_dispatch_delivery(
                    run_id=agent_run_id,
                    lease_seconds=60.0,
                )
            except Exception as e:
                logger.exception("Failed to claim durable conversation dispatch")
                raise HTTPException(
                    status_code=500,
                    detail="Failed to queue conversation dispatch",
                ) from e

            if delivery is not None:
                if trusted_legacy_marker is not None:
                    # This object-identity sentinel is process-local and must
                    # not be persisted in the JSON outbox payload.
                    delivery["payload"] = {
                        **dict(delivery.get("payload") or {}),
                        "_trusted_legacy": trusted_legacy_marker,
                    }
                try:
                    await server._queue_claimed_dispatch_delivery(
                        agent_run_service,
                        agent_run,
                        delivery,
                    )
                except Exception as e:
                    logger.exception("Failed to queue durable conversation dispatch")
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to queue conversation dispatch",
                    ) from e

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

        if not skip_user_persistence:
            try:
                from ...assistant.chat_turn_persistence import ChatTurnPersistence

                persisted_user_message = await ChatTurnPersistence().save_user_message(
                    session_id=session_id,
                    content=message,
                    metadata=message_metadata,
                    branch_from_message_id=payload.edit_message_id,
                    sender_type="user",
                    sender_id=user_id,
                    sender_display_name=sender_display_name,
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

        try:
            agent_run = await agent_run_service.create_run(
                session_id=session_id,
                user_id=user_id,
                project_id=effective_project_id,
                app_id=effective_app_id,
                app_target_id=effective_app_target_id,
                trigger_message_id=persisted_user_message_id,
                objective=message,
                run_type="chat_turn",
                generation_profile=generation_profile,
                metadata=run_metadata,
            )
        except Exception as e:
            logger.exception("Failed to create agent run for dispatch")
            raise HTTPException(
                status_code=500,
                detail="Failed to create agent run",
            ) from e
        agent_run_id = str(agent_run["id"])

        queued_payload = {
            "message": message,
            "session_id": session_id,
            "agent_run_id": agent_run_id,
            "_sender_user_id": user_id,
            "_sender_is_admin": bool(
                not getattr(server, "auth_enabled", True)
                or (user_info or {}).get("role") == "admin"
            ),
            "_sender_display_name": sender_display_name,
            "project_id": effective_project_id,
            "app_id": effective_app_id,
            "app_target_id": effective_app_target_id,
            "generation_profile": generation_profile,
            "planning_policy": planning_policy,
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
            "mentions": mentions,
            "_response_started_at_monotonic": time.monotonic(),
        }
        if trusted_legacy_marker is not None:
            queued_payload["_trusted_legacy"] = trusted_legacy_marker
        server._queue_user_message(queued_payload)

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
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Stop the active assistant generation for a conversation session."""
        user_info = await server._get_user_info_from_request(request)
        user_id = str((user_info or {}).get("id") or "default_user")
        if not await server._websocket_session_allowed(
            session_id,
            user_id,
            require_write=True,
            is_admin=bool(user_info and user_info.get("role") == "admin"),
        ):
            raise HTTPException(status_code=403, detail="Access denied")
        result = await server._handle_stop_generation({"session_id": session_id})
        return JSONResponse(
            {
                "success": not bool(result.get("persistence_failed")),
                **result,
            }
        )

    @app.get("/api/conversations/{session_id}/generation/status")
    async def get_conversation_generation_status(
        session_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Return the currently running generation status for a conversation."""
        user_info = await server._get_user_info_from_request(request)
        user_id = str((user_info or {}).get("id") or "default_user")
        if not await server._websocket_session_allowed(
            session_id,
            user_id,
        ):
            raise HTTPException(status_code=403, detail="Access denied")
        return JSONResponse(
            {"success": True, **server.get_conversation_generation_status(session_id)}
        )

    @app.post("/api/conversations/{session_id}/generation/steer")
    async def steer_conversation_generation(
        session_id: str,
        payload: UserMessage,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Interrupt the active generation with an extra instruction.

        This endpoint never queues: callers use it for Ctrl+Enter immediate
        interruption, and a missing active run is returned as a rejection.
        """
        user_info = await server._get_user_info_from_request(request)
        user_id = str((user_info or {}).get("id") or "default_user")
        if not await server._websocket_session_allowed(
            session_id,
            user_id,
            require_write=True,
            is_admin=bool(user_info and user_info.get("role") == "admin"),
        ):
            raise HTTPException(status_code=403, detail="Access denied")
        result = await server._handle_steer_generation(
            {
                "session_id": session_id,
                "message": payload.message,
                "client_message_id": payload.client_message_id,
                "agent_run_id": payload.agent_run_id,
                "_sender_user_id": user_id,
                "_sender_display_name": str(
                    (user_info or {}).get("display_name")
                    or (user_info or {}).get("username")
                    or user_id
                ),
            }
        )
        return JSONResponse({"success": True, **result})
