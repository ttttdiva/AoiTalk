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
        if not server._authorize_websocket(websocket):
            await websocket.close(code=1008)
            return

        ws_user_info = await server._get_user_info_from_websocket(websocket)
        ws_user_id = (
            str(ws_user_info.get("id"))
            if ws_user_info and ws_user_info.get("id")
            else "default_user"
        )
        ws_session_id = websocket.query_params.get("session_id")
        if ws_session_id and not await server._websocket_session_allowed(
            ws_session_id, ws_user_id
        ):
            await websocket.close(code=1008)
            return

        await server.manager.connect(
            websocket,
            user_id=ws_user_id,
            session_id=ws_session_id,
        )
        server._permission_broadcast_loop = asyncio.get_running_loop()

        # Set user context for os_operations permission checks
        await server._setup_user_context(websocket)

        try:
            while True:
                # Receive message from client
                data = await websocket.receive_json()
                message_type = data.get("type")

                if message_type == "user_message":
                    message_data = data.get("data", {}) or {}
                    if (
                        isinstance(message_data, dict)
                        and not message_data.get("session_id")
                    ):
                        if ws_session_id:
                            message_data = {
                                **message_data,
                                "session_id": ws_session_id,
                            }
                    if isinstance(message_data, dict):
                        message_data = {
                            **message_data,
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
                    await server._handle_user_message(message_data)
                elif message_type == "clear_chat":
                    await server._handle_clear_chat()
                elif message_type == "external_llm_permission_response":
                    await server._handle_external_llm_permission_response(
                        data.get("data", {})
                    )
                elif message_type == "external_model_prompt_response":
                    await server._handle_external_model_prompt_response(
                        data.get("data", {})
                    )
                elif message_type == "stop_generation":
                    control_data = data.get("data", {}) or {}
                    if isinstance(control_data, dict):
                        control_data = {
                            **control_data,
                            "session_id": control_data.get("session_id")
                            or websocket.query_params.get("session_id"),
                        }
                    await server._handle_stop_generation(control_data)
                elif message_type == "steer_generation":
                    control_data = data.get("data", {}) or {}
                    if isinstance(control_data, dict):
                        control_data = {
                            **control_data,
                            "session_id": control_data.get("session_id")
                            or websocket.query_params.get("session_id"),
                        }
                    await server._handle_steer_generation(control_data)
                elif message_type == "set_llm_mode":
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
