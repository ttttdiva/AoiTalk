"""モバイルクイックコマンド系ルート (server.py から移設)"""

from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .payloads import MobileCommandRequest

if TYPE_CHECKING:
    from ..server import WebChatServer


def register_mobile_command_routes(app: FastAPI, server: "WebChatServer") -> None:
    """モバイルコマンド一覧・実行ルートを登録する"""

    async def require_mobile_command_admin(request: Request) -> None:
        """Require admin role for configured mobile quick commands."""
        server._enforce_cookie_auth(request)
        if not await server._is_admin_user(request):
            raise HTTPException(
                status_code=403, detail="Administrator privileges required"
            )

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
        _: None = Depends(require_mobile_command_admin),
    ):
        """Execute a configured mobile command"""
        if not server._mobile_commands_enabled():
            raise HTTPException(
                status_code=403, detail="Mobile commands are disabled"
            )

        result = await server._execute_mobile_command(request.command_id)
        return JSONResponse(result)
