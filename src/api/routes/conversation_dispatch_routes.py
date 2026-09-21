"""会話メッセージの非同期ディスパッチ・生成制御ルート (server.py から移設)"""

import base64
import asyncio
import logging
import time
from collections.abc import Mapping
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


def _parse_builtin_masking_command(message: Any):
    """Return the canonical server-owned masking command, if present."""

    try:
        from ...services.masking_service import parse_masking_command

        return parse_masking_command(message)
    except Exception:
        return None


def _looks_like_builtin_masking_token(message: Any) -> bool:
    """Detect a leading literal token for fail-closed unavailable workers."""

    if not isinstance(message, str):
        return False
    parts = message.strip().split(None, 1)
    return bool(parts and parts[0].casefold() == "/masking")


def _parse_system_workflow_command(message: Any):
    """Parse the four server-owned workflow commands before Skill routing."""

    try:
        from ...services.workflow_controller import parse_workflow_command

        return parse_workflow_command(message)
    except Exception:
        return None


def _looks_like_system_workflow_token(message: Any) -> bool:
    if not isinstance(message, str):
        return False
    parts = message.strip().split(None, 1)
    return bool(
        parts
        and parts[0].casefold()
        in {"/document", "/template", "/app", "/macro"}
    )


async def _capture_learning_turn_best_effort(
    *,
    actor_id: str,
    raw_text: str,
    session_id: str,
    message_id: str,
    agent_run_id: str,
    project_id: str | None,
    client_message_id: str | None,
) -> None:
    from ...services.learning_capture_router import (
        DIRECT_WS_LEARNING_CAPTURE_TIMEOUT_SECONDS,
        LearningCaptureRouter,
    )

    try:
        await asyncio.wait_for(
            LearningCaptureRouter().capture_authenticated_turn(
                actor_id=str(actor_id),
                raw_text=str(raw_text),
                session_id=str(session_id),
                message_id=str(message_id),
                agent_run_id=str(agent_run_id),
                project_id=str(project_id) if project_id else None,
                client_message_id=str(client_message_id) if client_message_id else None,
            ),
            timeout=DIRECT_WS_LEARNING_CAPTURE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "REST learning capture timed out for canonical message=%s run=%s",
            message_id,
            agent_run_id,
        )
    except Exception:
        logger.exception(
            "Learning capture failed for canonical message=%s run=%s",
            message_id,
            agent_run_id,
        )


def _payload_field_was_provided(payload: Any, field_name: str) -> bool:
    """Support Pydantic v2 and v1 while preserving explicit nulls."""
    fields_set = getattr(payload, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(payload, "__fields_set__", set())
    return field_name in fields_set


def _message_field(value: Any, name: str, default: Any = None) -> Any:
    """Read ORM rows and mapping-shaped test/compatibility rows uniformly."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


async def _persist_existing_user_generation_profile(
    *,
    repository: Any,
    session_id: str,
    message_id: str,
    existing_message: Any,
    existing_metadata: Mapping[str, Any],
    generation_profile: str,
) -> None:
    """Merge the canonical profile into a pre-persisted user row.

    New-chat creation can persist the user row before dispatch (the REST
    request then carries ``skip_user_persistence``).  The dispatch payload is
    still the server authority for the resolved, allowlisted profile, but the
    modern conversation repository intentionally exposes no metadata-update
    helper.  Prefer a repository-provided updater for lightweight/legacy
    implementations and fall back to the persistence helper used by normal
    chat turns.  Finally keep mapping/object test doubles compatible without
    ever writing the untrusted raw profile value.
    """

    if existing_metadata.get("generation_profile") == generation_profile:
        return

    updater = getattr(repository, "update_message_metadata", None)
    if callable(updater):
        updated = await updater(
            session_id=session_id,
            message_id=message_id,
            updates={"generation_profile": generation_profile},
        )
        if updated is False:
            raise RuntimeError("persisted user message disappeared")
        return

    # ``ChatTurnPersistence`` owns the initialized memory repository in the
    # production path (which does provide ``update_message_metadata``).
    # Import lazily so compatibility tests and older deployments that do not
    # expose the helper can still dispatch the already validated row.
    try:
        from ...assistant.chat_turn_persistence import ChatTurnPersistence
    except (ImportError, ModuleNotFoundError):
        ChatTurnPersistence = None  # type: ignore[assignment,misc]

    if ChatTurnPersistence is not None:
        persistence = ChatTurnPersistence()
        updater = getattr(persistence, "update_message_metadata", None)
        if callable(updater):
            try:
                updated = await updater(
                    session_id=session_id,
                    message_id=message_id,
                    updates={"generation_profile": generation_profile},
                )
            except Exception:
                logger.warning(
                    "Failed to persist generation profile on existing user message %s",
                    message_id,
                    exc_info=True,
                )
                raise
            if updated is False:
                raise RuntimeError("persisted user message metadata update failed")
            return

    # A few legacy in-memory adapters expose only the loaded row.  Keep their
    # metadata projection coherent; the value is still the canonical server
    # result, never the caller's arbitrary string.
    merged_metadata = dict(existing_metadata)
    merged_metadata["generation_profile"] = generation_profile
    if isinstance(existing_message, Mapping):
        updated = False
        if "message_metadata" in existing_message:
            try:
                existing_message["message_metadata"] = merged_metadata  # type: ignore[index]
                updated = True
            except (TypeError, AttributeError):
                pass
        if "metadata" in existing_message:
            try:
                existing_message["metadata"] = merged_metadata  # type: ignore[index]
                updated = True
            except (TypeError, AttributeError):
                pass
        if updated:
            return
        try:
            existing_message["message_metadata"] = merged_metadata  # type: ignore[index]
            return
        except (TypeError, AttributeError):
            pass
    else:
        updated = False
        if hasattr(existing_message, "message_metadata") or not hasattr(
            existing_message, "metadata"
        ):
            try:
                setattr(existing_message, "message_metadata", merged_metadata)
                updated = True
            except (AttributeError, TypeError):
                pass
        if hasattr(existing_message, "metadata"):
            try:
                setattr(existing_message, "metadata", merged_metadata)
                updated = True
            except (AttributeError, TypeError):
                pass
        if updated:
            return

    raise RuntimeError("persisted user message metadata cannot be updated")


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
        masking_command = _parse_builtin_masking_command(message)
        masking_token_requested = _looks_like_builtin_masking_token(message)
        workflow_route = _parse_system_workflow_command(message)
        workflow_token_requested = _looks_like_system_workflow_token(message)
        raw_capabilities = sanitize_command_capabilities(payload.command_capabilities)
        message_tokens = message.split(None, 1)
        first_token = message_tokens[0].casefold() if message_tokens else ""
        help_requested = (
            "aoitalk_help" in raw_capabilities or first_token == "/help"
        )
        mentions = normalize_mentions(payload.mentions)
        # Idempotency follows the canonical structured target, not a mutable
        # client display label.  The full normalized payload is still retained
        # in the durable outbox for the common WebSocket resolver.
        mention_fingerprint = [
            {"type": item["type"], "id": item["id"]}
            for item in mentions
        ]
        if (
            not message
            and not payload.attachments
            and not payload.mentions
            and masking_command is None
            and not help_requested
        ):
            raise HTTPException(status_code=400, detail="message is required")
        # A masking request never needs an LLM callback.  If the built-in
        # parser/service is unavailable, fail closed instead of allowing the
        # raw slash command to fall through to normal generation.
        if (
            not server.on_user_input
            and not masking_token_requested
            and not workflow_token_requested
        ):
            raise HTTPException(
                status_code=503,
                detail="Conversation generation is not ready",
            )
        if masking_token_requested and masking_command is None:
            raise HTTPException(
                status_code=503,
                detail="Masking operation is not ready",
            )
        if workflow_token_requested and workflow_route is None:
            raise HTTPException(
                status_code=503,
                detail="System workflow is not ready",
            )
        try:
            # ``ConversationDispatchRequest`` is the normal Pydantic path,
            # while a few older adapters provide a small object without every
            # newer field (or pass the enum instance itself).  Resolve once at
            # the request boundary and only propagate this canonical value.
            raw_generation_profile = getattr(payload, "generation_profile", None)
            raw_generation_profile = getattr(
                raw_generation_profile,
                "value",
                raw_generation_profile,
            )
            generation_profile = resolve_generation_profile(
                raw_generation_profile
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
        # Help is intentionally one-turn only.  Do not resurrect it from an
        # edited/rerun source message unless this request explicitly selected
        # Help (or typed the reserved slash token again).  Other command
        # capabilities retain the existing edit inheritance behavior.
        if (
            payload.edit_message_id
            and "aoitalk_help" not in raw_capabilities
            and first_token != "/help"
        ):
            command_capabilities = tuple(
                capability
                for capability in command_capabilities
                if capability != "aoitalk_help"
            )
        help_requested = "aoitalk_help" in command_capabilities
        if generation_profile == "review":
            command_capabilities = filter_review_command_capabilities(
                command_capabilities
            )
        if masking_command is None and "work_intake" in command_capabilities:
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
        elif (
            masking_command is None
            and not message
            and not mentions
            and not help_requested
        ):
            raise HTTPException(status_code=400, detail="message is required")
        if help_requested:
            # Help must not resolve, attach, or mutate a selected Project.
            effective_attached_project_id = None
        else:
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
        effective_project_id = (
            None
            if help_requested
            else effective_attached_project_id
            or (str(conversation.project_id) if conversation.project_id else None)
        )
        effective_app_id = (
            None
            if help_requested
            else (
                str(payload.app_id) if payload.app_id else None
                if app_id_provided
                else stored_app_id
            )
        )
        effective_app_target_id = (
            None
            if help_requested
            else (
                str(payload.app_target_id) if payload.app_target_id else None
                if app_target_id_provided
                else stored_app_target_id
            )
        )
        if effective_app_id and not help_requested:
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
        elif app_scope_provided and not help_requested:
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
        if help_requested:
            include_project_context = False

        if masking_command is not None:
            # Direct/menu masking is completed synchronously by the trusted
            # request-boundary helper.  Do not create an AgentRun, outbox row,
            # learning capture, media-recognition task, or provider callback.
            # The normal WebSocket callback performs this write check before
            # masking interception; keep the REST boundary equivalent so a
            # read-only Project member cannot transform files from the
            # Project storage merely by posting a slash command.
            if effective_project_id and not (
                getattr(server, "auth_enabled", None) is False
                and trusted_legacy_marker is not None
            ):
                if not user_id or user_id == "default_user":
                    raise HTTPException(
                        status_code=403,
                        detail="Authenticated user identity is required",
                    )
                try:
                    from uuid import UUID

                    project_uuid = UUID(str(effective_project_id))
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid project id",
                    ) from None
                try:
                    checker = getattr(
                        server,
                        "_assert_project_write_access_for_turn",
                        None,
                    )
                    if not callable(checker):
                        raise RuntimeError("Project write access checker is unavailable")
                    await checker(project_uuid, user_id=user_id)
                except PermissionError as exc:
                    raise HTTPException(status_code=403, detail="Access denied") from exc
                except HTTPException:
                    raise
                except Exception as exc:
                    logger.warning("Masking Project write access check failed")
                    raise HTTPException(
                        status_code=500,
                        detail="Masking operation failed",
                    ) from exc
            masking_handler = getattr(server, "_execute_builtin_masking_turn", None)
            if not callable(masking_handler):
                raise HTTPException(
                    status_code=503,
                    detail="Masking operation is not ready",
                )
            sender_display_name = str(
                (user_info or {}).get("display_name")
                or (user_info or {}).get("username")
                or user_id
            )
            masking_payload = {
                # Preserve the raw request text in the audit row; the trusted
                # parser already normalizes only the command argument.
                "message": payload.message,
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
                "include_project_context": include_project_context,
                "edit_message_id": payload.edit_message_id,
                "client_message_id": payload.client_message_id,
                "attachments": payload.attachments or [],
                "skip_user_persistence": bool(
                    payload.skip_user_persistence
                ),
                "persisted_user_message_id": payload.persisted_user_message_id,
            }
            try:
                masking_result = await masking_handler(
                    masking_payload,
                    masking_command,
                )
            except PermissionError as exc:
                logger.warning("Masking dispatch authorization failed")
                raise HTTPException(status_code=403, detail="Access denied") from exc
            except ValueError as exc:
                logger.warning("Masking dispatch validation failed")
                raise HTTPException(
                    status_code=400,
                    detail="Invalid masking request",
                ) from exc
            except Exception as exc:
                logger.warning("Masking dispatch failed")
                raise HTTPException(
                    status_code=500,
                    detail="Masking operation failed",
                ) from exc
            response_payload = (
                dict(masking_result)
                if isinstance(masking_result, Mapping)
                else {}
            )
            # A malformed/legacy helper response is not a successful masking
            # result.  Keep the endpoint fail-closed rather than claiming
            # completion when no masked projection was returned.
            response_payload["success"] = response_payload.get("success") is True
            response_payload["queued"] = False
            response_payload["completed"] = response_payload["success"]
            return JSONResponse(response_payload, status_code=200)

        response_model = sanitize_response_model_selection(payload.response_model)
        run_metadata = {
            "client_message_id": payload.client_message_id,
            "generation_profile": generation_profile,
            "planning_policy": planning_policy,
            "include_project_context": include_project_context,
            "requested_include_project_context": (
                payload.include_project_context
            ),
            "command_capabilities": list(command_capabilities),
            "tools_required": payload.tools_required,
            "cloud_advisor_explicit": bool(payload.cloud_advisor_explicit),
            "edit_message_id": payload.edit_message_id,
            "response_model": response_model,
            "attachment_count": len(payload.attachments or []),
            "dispatch_source": "conversation_dispatch",
            "app_id": effective_app_id,
            "app_target_id": effective_app_target_id,
            "mention_count": len(mentions),
        }
        if workflow_route is not None:
            run_metadata["workflow"] = workflow_route.metadata
        agent_run_service = AgentRunService()
        sender_display_name = str(
            (user_info or {}).get("display_name")
            or (user_info or {}).get("username")
            or user_id
        )
        # Persist the server-resolved profile on every user row.  Branch/rerun
        # clients use this metadata as their authoritative selection; keeping
        # the raw request value out prevents an invalid or privileged string
        # from silently changing the next execution mode.
        message_metadata: Dict[str, Any] = {
            "generation_profile": generation_profile,
        }
        if payload.client_message_id:
            message_metadata["client_message_id"] = payload.client_message_id
        if payload.attachments:
            message_metadata["attachments"] = sanitize_chat_attachments(
                payload.attachments,
                include_binary=False,
            )
        if command_capabilities:
            message_metadata["command_capabilities"] = list(command_capabilities)
        if help_requested:
            # The Help user row is persisted before the worker runs on the
            # REST/outbox path.  Mark it at this server-owned boundary so the
            # one-turn history filter applies even when the worker receives
            # ``skip_user_persistence=True`` and cannot retrofit metadata.
            message_metadata["aoitalk_help"] = {
                "grounding": "pending",
                "one_turn": True,
            }
        if mentions:
            message_metadata["mentions"] = mentions
        if payload.cloud_advisor_explicit:
            message_metadata["cloud_advisor_explicit"] = True
        if workflow_route is not None:
            message_metadata["workflow"] = workflow_route.metadata

        if payload.persisted_user_message_id and not payload.skip_user_persistence:
            raise HTTPException(
                status_code=400,
                detail="persisted user message requires skip_user_persistence",
            )
        skip_user_persistence = bool(
            payload.skip_user_persistence and payload.persisted_user_message_id
        )
        # Keep the original meaning separate from the later lifecycle flag:
        # after a newly saved row is handed to the run, ``skip_user_persistence``
        # is also set to True, but that row already contains ``message_metadata``
        # and must not go through the pre-persisted-row updater below.
        pre_persisted_user_message = skip_user_persistence
        persisted_user_message_id = payload.persisted_user_message_id
        if skip_user_persistence:
            dispatch_repository = ConversationRepository()
            try:
                existing_message = await dispatch_repository.get_message_by_id(
                    persisted_user_message_id
                )
                existing_metadata = _message_field(
                    existing_message, "message_metadata", None
                )
                if not isinstance(existing_metadata, Mapping):
                    existing_metadata = _message_field(existing_message, "metadata", None)
                if not isinstance(existing_metadata, Mapping):
                    existing_metadata = {}
                stored_client_message_id = str(
                    _message_field(existing_message, "client_message_id", None)
                    or existing_metadata.get("client_message_id")
                    or ""
                ).strip()
                stored_capabilities = tuple(
                    sanitize_command_capabilities(
                        existing_metadata.get("command_capabilities")
                    )
                )
                requested_capabilities = tuple(command_capabilities)
                stored_help_marker = existing_metadata.get("aoitalk_help")
                if help_requested:
                    marker_valid = (
                        isinstance(stored_help_marker, Mapping)
                        and stored_help_marker.get("one_turn") is True
                        and str(stored_help_marker.get("grounding") or "").strip()
                        in {"pending", "ready", "unavailable"}
                    )
                else:
                    marker_valid = not isinstance(stored_help_marker, Mapping)
                if (
                    existing_message is None
                    or str(_message_field(existing_message, "session_id", ""))
                    != session_id
                    or str(_message_field(existing_message, "role", "")) != "user"
                    or str(_message_field(existing_message, "sender_type", "") or "").strip()
                    not in {"", "user"}
                    or str(_message_field(existing_message, "sender_id", "") or "").strip()
                    != str(user_id).strip()
                    or str(_message_field(existing_message, "content", "") or "").strip()
                    != message
                    or _message_field(existing_message, "deleted_at", None) is not None
                    or not payload.client_message_id
                    or stored_client_message_id != str(payload.client_message_id).strip()
                    or stored_capabilities != requested_capabilities
                    or not marker_valid
                    or sanitize_chat_attachments(
                        existing_metadata.get("attachments"),
                        include_binary=False,
                    )
                    != sanitize_chat_attachments(
                        payload.attachments or [],
                        include_binary=False,
                    )
                ):
                    raise ValueError("persisted user message does not match request")
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
                "cloud_advisor_explicit": bool(payload.cloud_advisor_explicit),
                "workflow": workflow_route.metadata if workflow_route is not None else None,
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
                    "cloud_advisor_explicit": bool(payload.cloud_advisor_explicit),
                    "workflow": workflow_route.metadata if workflow_route is not None else None,
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

            if pre_persisted_user_message:
                try:
                    await _persist_existing_user_generation_profile(
                        repository=dispatch_repository,
                        session_id=session_id,
                        message_id=str(persisted_user_message_id),
                        existing_message=existing_message,
                        existing_metadata=existing_metadata,
                        generation_profile=generation_profile,
                    )
                except Exception as e:
                    logger.exception(
                        "Failed to persist canonical generation profile for dispatch"
                    )
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to persist conversation dispatch metadata",
                    ) from e

            agent_run_id = str(agent_run["id"])
            # ``create_or_get_dispatch_turn`` returns the server-validated
            # canonical message/run pair for both first delivery and an
            # idempotent retry. Learning capture is itself message-idempotent,
            # so retry it even when the durable dispatch already existed; this
            # closes a transient first-capture failure without trusting any
            # client-owned message/run identity.
            if not help_requested:
                await _capture_learning_turn_best_effort(
                    actor_id=user_id,
                    raw_text=message,
                    session_id=session_id,
                    message_id=str(persisted_user_message_id),
                    agent_run_id=agent_run_id,
                    project_id=effective_project_id,
                    client_message_id=payload.client_message_id,
                )
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
        if pre_persisted_user_message:
            try:
                await _persist_existing_user_generation_profile(
                    repository=dispatch_repository,
                    session_id=session_id,
                    message_id=str(persisted_user_message_id),
                    existing_message=existing_message,
                    existing_metadata=existing_metadata,
                    generation_profile=generation_profile,
                )
            except Exception as e:
                logger.exception(
                    "Failed to persist canonical generation profile for dispatch"
                )
                raise HTTPException(
                    status_code=500,
                    detail="Failed to persist conversation dispatch metadata",
                ) from e
        agent_run_id = str(agent_run["id"])
        if not help_requested:
            await _capture_learning_turn_best_effort(
                actor_id=user_id,
                raw_text=message,
                session_id=session_id,
                message_id=str(persisted_user_message_id),
                agent_run_id=agent_run_id,
                project_id=effective_project_id,
                client_message_id=payload.client_message_id,
            )

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
            "cloud_advisor_explicit": bool(payload.cloud_advisor_explicit),
            "workflow": workflow_route.metadata if workflow_route is not None else None,
            "skip_user_persistence": skip_user_persistence,
            "persisted_user_message_id": persisted_user_message_id,
            "attachments": payload.attachments or [],
            "attachment_context": payload.attachment_context,
            "mentions": mentions,
            "_response_started_at_monotonic": time.monotonic(),
        }

        # Non-idempotent REST dispatches have no durable outbox lease, but the
        # persisted user row and AgentRun still form a server-owned hand-off.
        # Install the same lifecycle authority used by the outbox path so the
        # shared-group callback can safely reuse the canonical user row and so
        # every pre-handoff failure reaches a terminal AgentRun state instead
        # of remaining queued forever.
        lifecycle_lock = asyncio.Lock()
        lifecycle_state = {"settled": False}

        async def settle_terminal() -> bool:
            async with lifecycle_lock:
                lifecycle_state["settled"] = True
                return True

        async def settle_success(message: str) -> bool:
            async with lifecycle_lock:
                if lifecycle_state["settled"]:
                    return True
                completed = await agent_run_service.complete_run(
                    agent_run_id,
                    message=message,
                    result={"conversation_dispatch_completed": True},
                )
                if completed is None:
                    raise RuntimeError("agent run disappeared before dispatch completion")
                lifecycle_state["settled"] = True
                return True

        async def settle_failure(
            error: str = "Conversation dispatch failed before completion"
        ) -> bool:
            async with lifecycle_lock:
                if lifecycle_state["settled"]:
                    return True
                failed = await agent_run_service.fail_run(agent_run_id, error)
                if failed is None:
                    raise RuntimeError("agent run disappeared before dispatch failure")
                lifecycle_state["settled"] = True
                return True

        async def settle_cancelled() -> bool:
            async with lifecycle_lock:
                if lifecycle_state["settled"]:
                    return True
                cancelled = await agent_run_service.cancel_run(
                    agent_run_id,
                    message="Conversation generation stopped by user",
                )
                if cancelled is None:
                    raise RuntimeError("agent run disappeared before dispatch cancellation")
                lifecycle_state["settled"] = True
                return True

        queued_payload["_dispatch_delivery_lifecycle"] = {
            "agent_run_id": agent_run_id,
            # No outbox row exists for this legacy/non-idempotent path.  The
            # callback owns normal generation completion; this terminal hook
            # only records that no lease needs closing.
            "terminal": settle_terminal,
            "terminal_success": settle_success,
            "terminal_failure": settle_failure,
            "cancelled": settle_cancelled,
            "failure": settle_failure,
            "handed_off": False,
            "explicit_stop": False,
            "settlement": lifecycle_state,
        }
        if trusted_legacy_marker is not None:
            queued_payload["_trusted_legacy"] = trusted_legacy_marker
        try:
            accepted = server._queue_user_message(queued_payload)
        except BaseException:
            try:
                await settle_failure("Failed to queue conversation dispatch")
            except Exception:
                logger.exception("Failed to terminalize non-idempotent dispatch queue failure")
            raise
        if accepted is False:
            await settle_failure("Conversation dispatch was already queued")
            raise HTTPException(status_code=409, detail="Conversation dispatch was already queued")

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
