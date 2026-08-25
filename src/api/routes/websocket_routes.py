"""リアルタイム通信用 WebSocket エンドポイント (server.py から移設)"""

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# Import os_operations user context functions (server.py と同じフォールバック付き)
try:
    from ...tools.os_operations.tools import clear_user_context

    OS_OPS_CONTEXT_AVAILABLE = True
except ImportError:
    OS_OPS_CONTEXT_AVAILABLE = False
    clear_user_context = None

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_websocket_routes(app: FastAPI, server: "WebChatServer") -> None:
    """/ws WebSocket エンドポイントを登録する"""

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time communication"""
        auth_enabled = getattr(server, "auth_enabled", None)
        if auth_enabled is not True and auth_enabled is not False:
            # Authentication state is a security boundary.  Do not let an
            # uninitialized/unknown value fall through Python's truthiness
            # rules into the unauthenticated legacy path.
            logger.error("Refusing WebSocket connection with unknown auth state")
            await websocket.close(code=1011)
            return
        if not await server._authorize_websocket(websocket):
            await websocket.close(code=1008)
            return

        ws_user_info = await server._get_user_info_from_websocket(websocket)
        if server.auth_enabled is not False and not ws_user_info:
            # 認証有効時にユーザー解決へ失敗した接続を default_user として
            # 扱うと、reset-required/無効化済みユーザーがWSへ入れてしまう。
            await websocket.close(code=1008)
            return
        ws_user_id = (
            str(ws_user_info.get("id"))
            if ws_user_info and ws_user_info.get("id")
            else "default_user"
        )
        ws_session_id = websocket.query_params.get("session_id")
        if server.auth_enabled is True and not str(ws_session_id or "").strip():
            # Enterprise sockets are control channels, not a session-wide bus.
            # Requiring the session at handshake time prevents an authenticated
            # unbound client from selecting an arbitrary writable session in a
            # later stop/steer/permission payload.
            await websocket.close(code=1008)
            return
        if ws_session_id and not await server._websocket_session_allowed(
            ws_session_id,
            ws_user_id,
            is_admin=bool(ws_user_info and ws_user_info.get("role") == "admin"),
        ):
            await websocket.close(code=1008)
            return

        def payload_session_id(payload: object) -> str:
            if not isinstance(payload, dict):
                return ""
            value = payload.get("session_id")
            return str(value or "").strip()

        def bound_session_matches(payload: object) -> bool:
            """Prevent a session-bound socket from controlling another session."""
            if not ws_session_id:
                return True
            requested = payload_session_id(payload)
            return not requested or requested == ws_session_id

        await server.manager.connect(
            websocket,
            user_id=ws_user_id,
            session_id=ws_session_id,
            is_admin=bool(ws_user_info and ws_user_info.get("role") == "admin"),
            include_shared_history=server.auth_enabled is False,
        )
        server._permission_broadcast_loop = asyncio.get_running_loop()

        # Set user context for os_operations permission checks
        await server._setup_user_context(websocket, ws_user_info)

        try:
            while True:
                # Receive message from client
                data = await websocket.receive_json()
                if server.auth_enabled:
                    # アカウント無効化/reset要求を既存接続にも反映する。
                    # DB障害時もfail-closedで接続を閉じる。
                    fresh_user_info = await server._get_user_info_from_websocket(
                        websocket
                    )
                    if (
                        not fresh_user_info
                        or str(fresh_user_info.get("id")) != str(ws_user_id)
                    ):
                        await websocket.close(code=1008)
                        return
                    ws_user_info = fresh_user_info
                    await server._setup_user_context(websocket, ws_user_info)
                message_type = data.get("type")

                if message_type == "user_message":
                    message_data = data.get("data", {}) or {}
                    if not isinstance(message_data, dict):
                        await websocket.close(code=1008)
                        return
                    if (
                        not message_data.get("session_id")
                    ):
                        if ws_session_id:
                            message_data = {
                                **message_data,
                                "session_id": ws_session_id,
                            }
                    requested_session_id = str(
                        message_data.get("session_id") or ""
                    ).strip()
                    if ws_session_id and requested_session_id != ws_session_id:
                        await websocket.close(code=1008)
                        return
                    if server.auth_enabled and not requested_session_id:
                        # Enterprise WebSocket writes must always be scoped to a
                        # conversation.  Falling through to the legacy global
                        # handler would let a read-only user submit a message
                        # without hitting the session write check.
                        await websocket.close(code=1008)
                        return
                    if server.auth_enabled and requested_session_id:
                        if not await server._websocket_session_allowed(
                            requested_session_id,
                            ws_user_id,
                            require_write=True,
                            is_admin=bool(
                                ws_user_info and ws_user_info.get("role") == "admin"
                            ),
                        ):
                            await websocket.close(code=1008)
                            return
                    # Run IDs are server-owned.  Accepting a client-supplied
                    # ID would let a second WebSocket overwrite the existing
                    # cancellation handle and generation fence.
                    message_data.pop("agent_run_id", None)
                    generation_status = server.get_conversation_generation_status(
                        requested_session_id
                    )
                    if generation_status.get("status") in {
                        "cancellation_pending",
                        "cancellation_failed",
                    }:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "error": (
                                    "前の応答の停止処理が完了するまで"
                                    "新しい応答を開始できません"
                                ),
                                "status": generation_status.get("status"),
                                "session_id": requested_session_id,
                            }
                        )
                        continue
                    message_data = {
                        **message_data,
                        "_sender_user_id": ws_user_id,
                        "_sender_is_admin": bool(
                            not getattr(server, "auth_enabled", True)
                            or (ws_user_info and ws_user_info.get("role") == "admin")
                        ),
                        "_sender_display_name": (
                            str(
                                ws_user_info.get("display_name")
                                or ws_user_info.get("username")
                                or ws_user_id
                            )
                            if ws_user_info
                            else ws_user_id
                        ),
                    }
                    if server.auth_enabled is False:
                        # Keep the legacy no-auth compatibility path explicit.
                        # The marker is an object-identity sentinel and cannot
                        # be manufactured by JSON/WebSocket input.
                        from ..server_parts.conversation_mixin import (
                            TRUSTED_LEGACY_MARKER,
                        )

                        message_data["_trusted_legacy"] = TRUSTED_LEGACY_MARKER
                    await server._handle_user_message(message_data)
                elif message_type == "clear_chat":
                    if (
                        server.auth_enabled
                        and (not ws_user_info or ws_user_info.get("role") != "admin")
                    ):
                        await websocket.send_json(
                            {
                                "type": "error",
                                "error": "管理者のみ共有チャットをクリアできます",
                            }
                        )
                        continue
                    await server._handle_clear_chat(
                        user_id=ws_user_id,
                        session_id=ws_session_id,
                        admin_only=bool(server.auth_enabled),
                    )
                elif message_type == "external_llm_permission_response":
                    response_data = data.get("data", {}) or {}
                    if not isinstance(response_data, dict):
                        await websocket.close(code=1008)
                        return
                    if not bound_session_matches(response_data):
                        await websocket.close(code=1008)
                        return
                    response_session_id = str(
                        response_data.get("session_id") or ws_session_id or ""
                    ).strip() or None
                    if response_session_id and not await server._websocket_session_allowed(
                        response_session_id,
                        ws_user_id,
                        require_write=True,
                        is_admin=bool(ws_user_info and ws_user_info.get("role") == "admin"),
                    ):
                        await websocket.close(code=1008)
                        return
                    await server._handle_external_llm_permission_response(
                        response_data,
                        requester_user_id=ws_user_id,
                        requester_session_id=response_session_id,
                    )
                elif message_type == "external_model_prompt_response":
                    response_data = data.get("data", {}) or {}
                    if not isinstance(response_data, dict):
                        await websocket.close(code=1008)
                        return
                    if not bound_session_matches(response_data):
                        await websocket.close(code=1008)
                        return
                    response_session_id = str(
                        response_data.get("session_id") or ws_session_id or ""
                    ).strip() or None
                    if response_session_id and not await server._websocket_session_allowed(
                        response_session_id,
                        ws_user_id,
                        require_write=True,
                        is_admin=bool(ws_user_info and ws_user_info.get("role") == "admin"),
                    ):
                        await websocket.close(code=1008)
                        return
                    await server._handle_external_model_prompt_response(
                        response_data,
                        requester_user_id=ws_user_id,
                        requester_session_id=response_session_id,
                    )
                elif message_type in {
                    "human_interaction_response",
                    "ask_user_question_response",
                    "plan_approval_response",
                }:
                    response_data = data.get("data", {}) or {}
                    if not isinstance(response_data, dict):
                        await websocket.close(code=1008)
                        return
                    if not bound_session_matches(response_data):
                        await websocket.close(code=1008)
                        return
                    response_session_id = str(
                        response_data.get("session_id") or ws_session_id or ""
                    ).strip() or None
                    if response_session_id and not await server._websocket_session_allowed(
                        response_session_id,
                        ws_user_id,
                        require_write=True,
                        is_admin=bool(ws_user_info and ws_user_info.get("role") == "admin"),
                    ):
                        await websocket.close(code=1008)
                        return
                    await server._handle_human_interaction_response(
                        response_data,
                        requester_user_id=ws_user_id,
                        requester_session_id=response_session_id,
                    )
                elif message_type == "stop_generation":
                    raw_control_data = data.get("data", {}) or {}
                    control_data = (
                        raw_control_data
                        if isinstance(raw_control_data, dict)
                        else {}
                    )
                    if not bound_session_matches(control_data):
                        await websocket.close(code=1008)
                        return
                    requested_session_id = str(
                        control_data.get("session_id") or ws_session_id or ""
                    ).strip()
                    if (
                        not requested_session_id
                        or not await server._websocket_session_allowed(
                            requested_session_id,
                            ws_user_id,
                            require_write=True,
                            is_admin=bool(
                                ws_user_info and ws_user_info.get("role") == "admin"
                            ),
                        )
                    ):
                        await websocket.close(code=1008)
                        return
                    control_data = {
                        **control_data,
                        "session_id": requested_session_id,
                    }
                    await server._handle_stop_generation(control_data)
                elif message_type == "steer_generation":
                    raw_control_data = data.get("data", {}) or {}
                    control_data = (
                        raw_control_data
                        if isinstance(raw_control_data, dict)
                        else {}
                    )
                    if not bound_session_matches(control_data):
                        await websocket.close(code=1008)
                        return
                    requested_session_id = str(
                        control_data.get("session_id") or ws_session_id or ""
                    ).strip()
                    if (
                        not requested_session_id
                        or not await server._websocket_session_allowed(
                            requested_session_id,
                            ws_user_id,
                            require_write=True,
                            is_admin=bool(
                                ws_user_info and ws_user_info.get("role") == "admin"
                            ),
                        )
                    ):
                        await websocket.close(code=1008)
                        return
                    control_data = {
                        **control_data,
                        "session_id": requested_session_id,
                        "_sender_user_id": ws_user_id,
                        "_sender_display_name": (
                            str(
                                ws_user_info.get("display_name")
                                or ws_user_info.get("username")
                                or ws_user_id
                            )
                            if ws_user_info
                            else ws_user_id
                        ),
                    }
                    await server._handle_steer_generation(control_data)
                elif message_type == "set_llm_mode":
                    if (
                        server.auth_enabled
                        and (not ws_user_info or ws_user_info.get("role") != "admin")
                    ):
                        await websocket.send_json(
                            {
                                "type": "error",
                                "error": "管理者のみLLMモードを変更できます",
                            }
                        )
                        continue
                    await server._handle_set_llm_mode(data.get("data", {}))
                else:
                    logger.warning(f"Unknown message type: {message_type}")

        except WebSocketDisconnect:
            server.manager.disconnect(websocket)
            # Clear user context on disconnect
            if OS_OPS_CONTEXT_AVAILABLE and clear_user_context:
                clear_user_context()
        except (OSError, ConnectionError) as e:
            # Expected on Windows when client connection times out (WinError 121 semaphore timeout)
            logger.info(f"WebSocket connection lost: {e}")
            server.manager.disconnect(websocket)
            if OS_OPS_CONTEXT_AVAILABLE and clear_user_context:
                clear_user_context()
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            server.manager.disconnect(websocket)
            # Clear user context on error
            if OS_OPS_CONTEXT_AVAILABLE and clear_user_context:
                clear_user_context()
