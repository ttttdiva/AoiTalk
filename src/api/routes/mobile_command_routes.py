"""モバイルクイックコマンド系ルート (server.py から移設)"""

from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .payloads import MobileCommandRequest

if TYPE_CHECKING:
    from ..server import WebChatServer


def register_mobile_command_routes(app: FastAPI, server: "WebChatServer") -> None:
    """モバイルコマンド一覧・実行ルートを登録する"""

    async def require_mobile_command_admin(request: Request) -> dict[str, Any]:
        """Require admin role and resolve the server-side command identity."""
        server._enforce_cookie_auth(request)
        if not server.auth_enabled:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Mobile commands require an authenticated project user "
                    "when authentication is disabled"
                ),
            )

        user_info = await server._get_user_info_from_request(request)
        if not user_info or not user_info.get("id"):
            raise HTTPException(status_code=401, detail="Authentication required")
        # Use the principal resolved above.  Re-resolving from the request can
        # take a different cookie path (notably for Next.js sessions).
        if str(user_info.get("role") or "").strip().lower() != "admin":
            raise HTTPException(
                status_code=403, detail="Administrator privileges required"
            )
        return {
            "id": str(user_info["id"]).strip(),
            "role": str(user_info.get("role") or "user").strip().lower(),
        }

    @app.get("/api/mobile/commands")
    async def get_mobile_commands(_: None = Depends(require_mobile_command_admin)):
        """Return mobile quick command metadata"""
        if not server._mobile_commands_enabled():
            return JSONResponse({"enabled": False, "commands": []})

        return JSONResponse(
            {
                "enabled": True,
                "default_view": server.mobile_ui_config.get("default_view", "chat"),
                "commands": server._serialize_mobile_commands(),
            }
        )

    @app.post("/api/mobile/commands/run")
    async def run_mobile_command(
        request: MobileCommandRequest,
        user_info: dict[str, Any] = Depends(require_mobile_command_admin),
    ):
        """Execute a configured mobile command"""
        if not server._mobile_commands_enabled():
            raise HTTPException(
                status_code=403, detail="Mobile commands are disabled"
            )

        result = await server._execute_mobile_command(
            request.command_id,
            sender_user_id=user_info["id"],
            sender_is_admin=user_info["role"] == "admin",
        )
        return JSONResponse(result)
