"""MCP 管理ルート。"""

from __future__ import annotations

import logging
import os
import platform
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def build_mcp_router(app_instance: Any, require_auth: Callable[..., Any]) -> APIRouter:
    """MCP 管理の APIRouter を構築する。"""

    mcp_router = APIRouter(
        prefix="/api/mcp",
        tags=["mcp"],
    )

    def _get_mcp_plugin():
        """app_instance から MCPPlugin を取得する。"""
        plugin = getattr(app_instance, "mcp_plugin", None)
        if plugin is None:
            raise HTTPException(
                status_code=503,
                detail="MCPプラグインが利用できません",
            )
        return plugin

    @mcp_router.get("/servers")
    async def list_mcp_servers(
        request: Request,
        _=Depends(require_auth),
    ):
        """MCPサーバー一覧をステータス付きで取得"""
        try:
            plugin = _get_mcp_plugin()
            server_info = plugin.client.get_server_info()
            servers = []
            for name, info in server_info.items():
                is_connected = name in plugin.client.sessions
                servers.append(
                    {
                        "name": name,
                        "status": "connected" if is_connected else "disconnected",
                        "info": info,
                    }
                )
            return JSONResponse(
                content={
                    "success": True,
                    "servers": servers,
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCPサーバー一覧取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @mcp_router.get("/servers/{name}/tools")
    async def list_mcp_server_tools(
        name: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """指定サーバーのツール一覧を取得"""
        try:
            plugin = _get_mcp_plugin()
            if name not in plugin.client.sessions:
                raise HTTPException(
                    status_code=404,
                    detail=f"MCPサーバー '{name}' が見つかりません",
                )
            tools = await plugin.client.list_tools(server_name=name)
            return JSONResponse(
                content={
                    "success": True,
                    "server": name,
                    "tools": tools.get(name, []),
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCPツール一覧取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @mcp_router.post("/servers/{name}/toggle")
    async def toggle_mcp_server(
        name: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """MCPサーバーの有効/無効を切り替え"""
        try:
            plugin = _get_mcp_plugin()
            if name in plugin.client.sessions:
                # 接続中 → 切断
                await plugin.client.remove_server(name)
                return JSONResponse(
                    content={
                        "success": True,
                        "server": name,
                        "status": "disconnected",
                        "message": f"サーバー '{name}' を切断しました",
                    }
                )
            else:
                # 切断中 → 再接続を試みる
                server_info = plugin.client.servers.get(name)
                if server_info is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"MCPサーバー '{name}' の設定が見つかりません",
                    )
                return JSONResponse(
                    content={
                        "success": False,
                        "server": name,
                        "message": "サーバーの再接続にはリスタートを使用してください",
                    }
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCPサーバートグルエラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @mcp_router.post("/servers/{name}/restart")
    async def restart_mcp_server(
        name: str,
        request: Request,
        _=Depends(require_auth),
    ):
        """MCPサーバーを再起動"""
        try:
            plugin = _get_mcp_plugin()
            # 既存接続を切断
            if (
                name in plugin.client.sessions
                or name in getattr(plugin.client, "servers", {})
                or name in getattr(plugin.client, "_server_scopes", {})
            ):
                await plugin.client.remove_server(name)

            # サーバー設定を取得して再接続
            config = getattr(app_instance, "config", None) or {}
            mcp_config = config.get("mcp", {})
            if isinstance(mcp_config, dict):
                mcp_config = mcp_config.get("servers")
            else:
                mcp_config = None
            if mcp_config is None:
                # Config supports dotted keys while lightweight test/config
                # adapters often expose only ``mcp.servers`` directly.
                mcp_config = config.get("mcp.servers", {})
            if mcp_config is None:
                mcp_config = {}
            if not isinstance(mcp_config, dict):
                mcp_config = {}
            server_config = mcp_config.get(name)

            if server_config is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"MCPサーバー '{name}' の設定が見つかりません",
                )

            if isinstance(server_config, dict) and (
                "windows" in server_config or "linux" in server_config
            ):
                platform_name = "windows" if platform.system() == "Windows" else "linux"
                if platform_name in server_config:
                    shared_env = server_config.get("env", {}) or {}
                    actual_config = dict(server_config[platform_name])
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"プラットフォーム '{platform_name}' の設定がありません",
                    )
            else:
                shared_env = {}
                actual_config = dict(server_config)

            # Expand only environment values explicitly configured for this
            # server.  ``MCPPlugin.add_server`` applies the fixed child
            # process allowlist and avoids inheriting the parent environment.
            env = {**shared_env, **(actual_config.get("env", {}) or {})}
            expanded_env = {}
            for key, value in env.items():
                if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                    expanded_env[key] = os.getenv(value[2:-1], "")
                else:
                    expanded_env[key] = value

            success = await plugin.add_server(
                name=name,
                command=actual_config.get("command"),
                args=actual_config.get("args", []),
                env=expanded_env,
            )

            if success:
                return JSONResponse(
                    content={
                        "success": True,
                        "server": name,
                        "status": "connected",
                        "message": f"サーバー '{name}' を再起動しました",
                    }
                )
            else:
                raise HTTPException(
                    status_code=500,
                    detail=f"サーバー '{name}' の再起動に失敗しました",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCPサーバー再起動エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @mcp_router.get("/status")
    async def get_mcp_status(
        request: Request,
        _=Depends(require_auth),
    ):
        """MCP全体のヘルスステータスを取得"""
        try:
            plugin = _get_mcp_plugin()
            is_available = plugin.is_available()
            is_initialized = plugin.is_initialized()
            server_info = plugin.client.get_server_info()
            connected_count = len(plugin.client.sessions)
            total_count = len(server_info)

            return JSONResponse(
                content={
                    "success": True,
                    "status": {
                        "available": is_available,
                        "initialized": is_initialized,
                        "total_servers": total_count,
                        "connected_servers": connected_count,
                        "health": (
                            "healthy"
                            if connected_count == total_count and total_count > 0
                            else "degraded" if connected_count > 0 else "unavailable"
                        ),
                    },
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("MCPステータス取得エラー: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return mcp_router
