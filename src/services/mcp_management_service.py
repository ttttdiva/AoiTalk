"""MCPサーバー管理サービス

MCPプラグインシステムのステータス確認・制御機能を提供する。
設定ファイル(config.yaml)のmcp.servers セクションに基づき、
各MCPサーバーの状態取得・再起動・ヘルスチェックを行う。
"""

from __future__ import annotations

import logging
import platform
import os
from typing import Any, Dict, List, Optional

from ..utils.subprocess_env import build_aoitalk_subprocess_env

logger = logging.getLogger(__name__)


class MCPManagementService:
    """MCPサーバーの管理・監視サービス

    MCPPlugin インスタンスと Config オブジェクトを組み合わせて、
    設定済みサーバーの一覧取得、個別ツール一覧、ヘルスステータス、
    およびサーバー再起動を提供する。
    """

    def __init__(self, mcp_plugin=None):
        """初期化

        Args:
            mcp_plugin: MCPPlugin インスタンス。None の場合はグローバルから取得を試みる。
        """
        self._plugin = mcp_plugin

    @property
    def plugin(self):
        """遅延取得対応の MCPPlugin アクセサ"""
        return self._plugin

    def _get_configured_servers(self, config) -> Dict[str, Dict[str, Any]]:
        """設定ファイルから MCP サーバー定義を取得する。

        Args:
            config: Config オブジェクト（get メソッドでドット記法アクセス可能）。

        Returns:
            サーバー名をキー、設定辞書を値とする辞書。
        """
        servers = config.get("mcp.servers", {})
        if servers is None:
            return {}
        return servers

    def _resolve_server_config(self, server_config: Dict[str, Any]) -> Dict[str, Any]:
        """プラットフォーム固有設定を解決する。

        Args:
            server_config: サーバー設定辞書（windows/linux キーを含む場合がある）。

        Returns:
            現在のプラットフォームに適した設定辞書。
        """
        if isinstance(server_config, dict) and (
            "windows" in server_config or "linux" in server_config
        ):
            platform_name = "windows" if platform.system() == "Windows" else "linux"
            return server_config.get(platform_name, {})
        return server_config

    def _detect_server_status(self, server_name: str) -> str:
        """サーバーの稼働状態を判定する。

        Returns:
            "running" | "stopped" | "error"
        """
        plugin = self.plugin
        if plugin is None or not plugin.is_initialized():
            return "stopped"

        client = plugin.client

        # セッションが存在する = プロセスが起動済み
        if server_name not in client.sessions:
            return "stopped"

        # セッションは存在するが、疎通確認
        try:
            session = client.sessions[server_name]
            # セッションオブジェクトが None でない且つ使えそうならrunning
            if session is not None:
                return "running"
            return "error"
        except Exception:
            return "error"

    def _get_last_error(self, server_name: str) -> Optional[str]:
        """サーバーの直近エラー情報を取得する（存在すれば）。"""
        # MCPClient は明示的なエラーストレージを持たないため、
        # ステータスが error の場合のみ汎用メッセージを返す
        status = self._detect_server_status(server_name)
        if status == "error":
            return f"サーバー '{server_name}' でエラーが検出されました"
        return None

    # ────────────────────────────────────────────
    # 公開 API
    # ────────────────────────────────────────────

    def get_server_list(self, config) -> List[Dict[str, Any]]:
        """設定済みの全MCPサーバーを一覧取得する。

        Args:
            config: Config オブジェクト。

        Returns:
            各サーバーの name, command, args, status, error を含む辞書リスト。
        """
        configured = self._get_configured_servers(config)
        result: List[Dict[str, Any]] = []

        for name, raw_config in configured.items():
            resolved = self._resolve_server_config(raw_config)
            command = resolved.get("command", "")
            args = resolved.get("args", [])
            status = self._detect_server_status(name)
            error = self._get_last_error(name)

            entry = {
                "name": name,
                "command": command,
                "args": args,
                "status": status,
            }
            if error:
                entry["error"] = error

            result.append(entry)

        logger.debug("MCPサーバー一覧を取得: %d 件", len(result))
        return result

    async def get_server_tools(self, server_name: str, config) -> List[Dict[str, Any]]:
        """指定サーバーが提供するツール一覧を取得する。

        Args:
            server_name: サーバー名。
            config: Config オブジェクト。

        Returns:
            ツール定義のリスト（name, description, inputSchema）。
            サーバーが停止中または未接続の場合は空リスト。
        """
        plugin = self.plugin
        if plugin is None or not plugin.is_initialized():
            logger.warning("MCPプラグインが初期化されていません")
            return []

        client = plugin.client
        if server_name not in client.sessions:
            logger.warning("サーバー '%s' は接続されていません", server_name)
            return []

        try:
            tools_by_server = await client.list_tools(server_name)
            tools = tools_by_server.get(server_name, [])
            logger.info("サーバー '%s' のツール一覧を取得: %d 件", server_name, len(tools))
            return tools
        except Exception as e:
            logger.error("サーバー '%s' のツール取得に失敗: %s", server_name, e)
            return []

    def get_health_status(self, config) -> Dict[str, Any]:
        """MCPシステム全体のヘルスステータスを取得する。

        Args:
            config: Config オブジェクト。

        Returns:
            total, running, stopped, errors の各カウントと詳細を含む辞書。
        """
        servers = self.get_server_list(config)
        total = len(servers)
        running = sum(1 for s in servers if s["status"] == "running")
        stopped = sum(1 for s in servers if s["status"] == "stopped")
        errors = sum(1 for s in servers if s["status"] == "error")

        error_details = [
            {"name": s["name"], "error": s.get("error", "不明なエラー")}
            for s in servers
            if s["status"] == "error"
        ]

        health = {
            "total": total,
            "running": running,
            "stopped": stopped,
            "errors": errors,
            "error_details": error_details,
            "healthy": errors == 0 and running == total,
        }

        logger.debug(
            "MCPヘルスステータス: total=%d, running=%d, stopped=%d, errors=%d",
            total, running, stopped, errors,
        )
        return health

    async def restart_server(self, server_name: str, config) -> Dict[str, Any]:
        """指定サーバーを再起動する。

        既存セッションを削除後、設定に基づいてサーバーを再接続する。

        Args:
            server_name: 再起動するサーバー名。
            config: Config オブジェクト。

        Returns:
            再起動結果を含む辞書（success, name, status, error）。
        """
        plugin = self.plugin
        if plugin is None or not plugin.is_initialized():
            msg = "MCPプラグインが初期化されていません"
            logger.error(msg)
            return {"success": False, "name": server_name, "status": "stopped", "error": msg}

        # 設定から対象サーバーの定義を取得
        configured = self._get_configured_servers(config)
        if server_name not in configured:
            msg = f"サーバー '{server_name}' は設定に存在しません"
            logger.error(msg)
            return {"success": False, "name": server_name, "status": "stopped", "error": msg}

        raw_config = configured[server_name]

        logger.info("MCPサーバー '%s' を再起動します", server_name)

        # 1. 既存セッションの削除
        try:
            await plugin.client.remove_server(server_name)
            logger.debug("サーバー '%s' のセッションを削除しました", server_name)
        except Exception as e:
            logger.warning("セッション削除中にエラー（続行します）: %s", e)

        # 2. プラットフォーム固有設定を解決
        shared_env = {}
        if isinstance(raw_config, dict) and (
            "windows" in raw_config or "linux" in raw_config
        ):
            shared_env = raw_config.get("env", {})
            platform_name = "windows" if platform.system() == "Windows" else "linux"
            if platform_name not in raw_config:
                msg = f"プラットフォーム '{platform_name}' の設定がありません"
                logger.error(msg)
                return {"success": False, "name": server_name, "status": "stopped", "error": msg}
            actual_config = dict(raw_config[platform_name])
        else:
            actual_config = dict(raw_config)

        # 3. 環境変数の構築。親AoiTalk process全体は継承せず、設定に
        # 明示された値だけを子MCPへ渡す。
        env = {**shared_env, **actual_config.get("env", {})}
        expanded_env: Dict[str, str] = {}
        for key, value in env.items():
            if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                env_var_name = value[2:-1]
                expanded_env[key] = os.getenv(env_var_name, "")
            else:
                expanded_env[key] = value
        child_env = build_aoitalk_subprocess_env(
            extra_env=expanded_env,
            sensitive_env_keys=expanded_env,
        )

        # 4. サーバーを再接続
        try:
            success = await plugin.add_server(
                name=server_name,
                command=actual_config.get("command"),
                args=actual_config.get("args", []),
                env=child_env,
            )
            if success:
                logger.info("MCPサーバー '%s' の再起動に成功しました", server_name)
                return {"success": True, "name": server_name, "status": "running"}
            else:
                msg = f"サーバー '{server_name}' の接続に失敗しました"
                logger.error(msg)
                return {"success": False, "name": server_name, "status": "error", "error": msg}
        except Exception as e:
            msg = f"サーバー '{server_name}' の再起動中にエラー: {e}"
            logger.error(msg)
            return {"success": False, "name": server_name, "status": "error", "error": msg}
