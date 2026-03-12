"""
コメント管理ツール: add_comment, get_task_comments

新規ツール（ClickUp API v2 /task/{task_id}/comment エンドポイント使用）
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..api_client import ClickUpAPIClient
    from ..db_client import ClickUpDBClient


def register(mcp: FastMCP, api: ClickUpAPIClient, db: ClickUpDBClient):
    """コメント管理ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def add_comment(task_id: str, comment_text: str) -> str:
        """タスクにコメントを追加します。

        Args:
            task_id: タスクID
            comment_text: コメント内容
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        if not comment_text.strip():
            return "エラー: コメント内容が空です"

        resp = await api.post(
            f"/task/{task_id}/comment",
            json={"comment_text": comment_text},
        )

        if resp.status_code in (200, 201):
            return f"タスク {task_id} にコメントを追加しました"
        else:
            err = resp.text
            try:
                err = resp.json().get("err", err)
            except Exception:
                pass
            return f"エラー: コメント追加に失敗しました - {err}"

    @mcp.tool()
    async def get_task_comments(task_id: str) -> str:
        """タスクのコメント一覧を取得します。

        Args:
            task_id: タスクID
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        resp = await api.get(f"/task/{task_id}/comment")

        if resp.status_code != 200:
            err = resp.text
            try:
                err = resp.json().get("err", err)
            except Exception:
                pass
            return f"エラー: コメント取得に失敗しました - {err}"

        comments = resp.json().get("comments", [])
        if not comments:
            return f"タスク {task_id} にコメントはありません"

        lines = [f"タスク {task_id} のコメント ({len(comments)}件):"]
        for comment in comments:
            lines.append("")

            # 投稿者
            user = comment.get("user", {})
            username = user.get("username", user.get("email", "不明"))
            lines.append(f"  投稿者: {username}")

            # 日時
            if comment.get("date"):
                try:
                    dt = datetime.fromtimestamp(int(comment["date"]) / 1000)
                    lines.append(f"  日時: {dt.strftime('%Y-%m-%d %H:%M')}")
                except (ValueError, TypeError):
                    pass

            # コメント本文
            text = comment.get("comment_text", "")
            if not text and comment.get("comment"):
                # comment フィールドはリッチテキスト形式の場合がある
                parts = comment["comment"]
                if isinstance(parts, list):
                    text = "".join(
                        p.get("text", "") for p in parts if isinstance(p, dict)
                    )
                elif isinstance(parts, str):
                    text = parts

            if text:
                # 長いコメントは切り詰め
                if len(text) > 300:
                    text = text[:300] + "..."
                lines.append(f"  内容: {text}")

        return "\n".join(lines)
