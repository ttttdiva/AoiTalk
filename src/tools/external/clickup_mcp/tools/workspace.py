"""
ワークスペース関連ツール: get_teams, get_spaces, get_folders,
                         get_lists, get_folder_lists
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..api_client import ClickUpAPIClient
    from ..db_client import ClickUpDBClient


def register(mcp: FastMCP, api: ClickUpAPIClient, db: ClickUpDBClient):
    """ワークスペース関連ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def get_teams() -> str:
        """ClickUpのチーム（ワークスペース）一覧を取得します。"""
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        resp = await api.get("/team")
        if resp.status_code != 200:
            return f"エラー: チーム取得に失敗しました (ステータスコード: {resp.status_code})"

        teams = resp.json().get("teams", [])
        if not teams:
            return "チームが見つかりません"

        lines = ["利用可能なチーム:"]
        for team in teams:
            lines.append(f"- {team['name']} (ID: {team['id']})")
        return "\n".join(lines)

    @mcp.tool()
    async def get_spaces(team_id: Optional[str] = None) -> str:
        """チーム内のスペース一覧を取得します。

        Args:
            team_id: チームID（省略時は環境変数のTEAM_IDを使用）
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        target = team_id or api.team_id
        if not target:
            return "エラー: team_idが指定されていません（環境変数CLICKUP_TEAM_IDも未設定）"

        resp = await api.get(f"/team/{target}/space")
        if resp.status_code != 200:
            return f"エラー: スペース取得に失敗しました (ステータスコード: {resp.status_code})"

        spaces = resp.json().get("spaces", [])
        if not spaces:
            return "スペースが見つかりません"

        lines = [f"チーム {target} のスペース:"]
        for space in spaces:
            lines.append(f"- {space['name']} (ID: {space['id']})")
            statuses = space.get("statuses")
            if statuses:
                status_names = ", ".join(s["status"] for s in statuses)
                lines.append(f"  ステータス: {status_names}")
        return "\n".join(lines)

    @mcp.tool()
    async def get_folders(space_id: str) -> str:
        """スペース内のフォルダー一覧を取得します。

        Args:
            space_id: スペースID
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        resp = await api.get(f"/space/{space_id}/folder")
        if resp.status_code != 200:
            return f"エラー: フォルダー取得に失敗しました (ステータスコード: {resp.status_code})"

        folders = resp.json().get("folders", [])
        if not folders:
            return f"スペース {space_id} にフォルダーが見つかりません"

        lines = [f"スペース {space_id} のフォルダー:"]
        for folder in folders:
            lines.append(f"- {folder['name']} (ID: {folder['id']})")
            folder_lists = folder.get("lists", [])
            if folder_lists:
                lines.append(f"  リスト数: {len(folder_lists)}")
        return "\n".join(lines)

    @mcp.tool()
    async def get_lists(space_id: str) -> str:
        """スペース内のフォルダーに属さないリスト一覧を取得します。

        Args:
            space_id: スペースID
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        resp = await api.get(f"/space/{space_id}/list")
        if resp.status_code != 200:
            return f"エラー: リスト取得に失敗しました (ステータスコード: {resp.status_code})"

        lists = resp.json().get("lists", [])
        if not lists:
            return f"スペース {space_id} にリストが見つかりません"

        lines = [f"スペース {space_id} のリスト:"]
        for lst in lists:
            lines.append(f"- {lst['name']} (ID: {lst['id']})")
            if lst.get("task_count") is not None:
                lines.append(f"  タスク数: {lst['task_count']}")
        return "\n".join(lines)

    @mcp.tool()
    async def get_folder_lists(folder_id: str) -> str:
        """フォルダー内のリスト一覧を取得します。

        Args:
            folder_id: フォルダーID
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        resp = await api.get(f"/folder/{folder_id}/list")
        if resp.status_code != 200:
            return f"エラー: フォルダー内リスト取得に失敗しました (ステータスコード: {resp.status_code})"

        lists = resp.json().get("lists", [])
        if not lists:
            return f"フォルダー {folder_id} にリストが見つかりません"

        lines = [f"フォルダー {folder_id} のリスト:"]
        for lst in lists:
            lines.append(f"- {lst['name']} (ID: {lst['id']})")
            if lst.get("task_count") is not None:
                lines.append(f"  タスク数: {lst['task_count']}")
        return "\n".join(lines)
