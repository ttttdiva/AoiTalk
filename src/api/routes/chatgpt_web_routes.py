"""Director 用 ChatGPT Web 接続の管理 API。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ...llm.chatgpt_web_provider import (
    ChatGPTWebBusyError,
    ChatGPTWebError,
    ChatGPTWebNeedsHumanError,
    ChatGPTWebProvider,
    chatgpt_web_status,
    close_chatgpt_web_resources,
    open_chatgpt_settings_browser,
)
from ..router_helpers import cookie_auth_dependency

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


def register_chatgpt_web_routes(app: FastAPI, server: "WebChatServer") -> None:
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    shutdown_hooks = getattr(server, "_shutdown_background_tasks", None)
    if isinstance(shutdown_hooks, list):
        shutdown_hooks.append(close_chatgpt_web_resources)

    @app.get("/api/chatgpt-web/status")
    async def get_chatgpt_web_status(_: None = Depends(require_auth)):
        return JSONResponse(chatgpt_web_status(server.config))

    @app.post("/api/chatgpt-web/settings-browser")
    async def start_chatgpt_web_settings_browser(
        _: None = Depends(require_auth),
    ):
        try:
            status = await open_chatgpt_settings_browser(server.config)
            return JSONResponse(
                {
                    **status,
                    "message": (
                        "ChatGPT設定ブラウザを開きました。"
                        "ログインとモデル設定を済ませ、完了後にウィンドウを閉じてください。"
                    ),
                }
            )
        except ChatGPTWebBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ChatGPTWebError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        except Exception as exc:
            logger.exception("ChatGPT設定ブラウザの起動に失敗")
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/chatgpt-web/check-login")
    async def check_chatgpt_web_login(_: None = Depends(require_auth)):
        provider = ChatGPTWebProvider(server.config)
        try:
            logged_in = await provider.check_login()
            return JSONResponse(
                {
                    **chatgpt_web_status(server.config),
                    "logged_in": logged_in,
                    "message": (
                        "ChatGPTにログイン済みです。"
                        if logged_in
                        else "ChatGPTにログインしていません。設定ブラウザからログインしてください。"
                    ),
                }
            )
        except ChatGPTWebBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ChatGPTWebNeedsHumanError as exc:
            return JSONResponse(
                {
                    **chatgpt_web_status(server.config),
                    "logged_in": False,
                    "needs_human": True,
                    "message": str(exc),
                }
            )
        except ChatGPTWebError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
