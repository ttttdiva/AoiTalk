"""
検索・サマリーツール: search_tasks, get_weekly_summary
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..api_client import ClickUpAPIClient
    from ..db_client import ClickUpDBClient

from ..api_client import parse_datetime_to_ms


def register(mcp: FastMCP, api: ClickUpAPIClient, db: ClickUpDBClient):
    """検索・サマリーツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def search_tasks(
        team_id: Optional[str] = None,
        query: Optional[str] = None,
        assignees: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        start_date_gt: Optional[str] = None,
        start_date_lt: Optional[str] = None,
        exclude_closed: bool = True,
        exclude_no_start_date: bool = False,
    ) -> str:
        """タスクを検索します。

        Args:
            team_id: チームID（省略時は環境変数のTEAM_IDを使用）
            query: 検索クエリ
            assignees: 担当者でフィルタ
            statuses: ステータスでフィルタ
            tags: タグでフィルタ
            start_date_gt: 開始日がこの日付より後のタスクを検索 (YYYY-MM-DD)
            start_date_lt: 開始日がこの日付より前のタスクを検索 (YYYY-MM-DD)
            exclude_closed: Closedステータスのタスクを除外する（デフォルト: True）
            exclude_no_start_date: 開始日が未設定のタスクを除外する（デフォルト: False）
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        target_team = team_id or api.team_id
        if not target_team:
            return "エラー: team_idが指定されていません（環境変数CLICKUP_TEAM_IDも未設定）"

        # まずDBでローカル検索を試みる（高速）
        if query and db.enabled:
            db_results = await asyncio.to_thread(db.search_tasks_by_name, query)
            if db_results:
                lines = [f"ローカルDB検索結果 ({len(db_results)}件):"]
                for task in db_results:
                    lines.append(f"\n- {task['name']} (ID: {task['id']})")
                    if task.get("status"):
                        lines.append(f"  ステータス: {task['status']}")
                    if task.get("list_name"):
                        lines.append(f"  リスト: {task['list_name']}")
                    if task.get("due_date"):
                        lines.append(f"  期限: {task['due_date'].strftime('%Y-%m-%d')}")
                lines.append("\n(※ ローカルキャッシュの結果です。最新情報はAPIから取得中...)")

        # API検索（自動ページネーション）
        # ClickUp API は配列パラメータに key[] 形式を要求する
        base_params: list[tuple[str, str]] = [("team_id", target_team)]
        if query:
            base_params.append(("query", query))
        if assignees:
            for a in assignees:
                base_params.append(("assignees[]", a))
        if statuses:
            for s in statuses:
                base_params.append(("statuses[]", s))
        if tags:
            for t in tags:
                base_params.append(("tags[]", t))
        if start_date_gt:
            ts, _ = parse_datetime_to_ms(start_date_gt)
            if ts is not None:
                base_params.append(("start_date_gt", str(ts)))
        if start_date_lt:
            ts, _ = parse_datetime_to_ms(start_date_lt)
            if ts is not None:
                base_params.append(("start_date_lt", str(ts)))
        if not exclude_closed:
            base_params.append(("include_closed", "true"))

        all_tasks: list[dict] = []
        max_pages = 10
        for page in range(max_pages):
            params = base_params + [("page", str(page))]
            resp = await api.get(f"/team/{target_team}/task", params=params)
            if resp.status_code != 200:
                if page == 0:
                    return f"エラー: タスク検索に失敗しました (ステータスコード: {resp.status_code})"
                break
            page_tasks = resp.json().get("tasks", [])
            all_tasks.extend(page_tasks)
            if len(page_tasks) < 100:
                break

        # クライアント側フィルタ
        if exclude_no_start_date:
            all_tasks = [t for t in all_tasks if t.get("start_date")]

        if not all_tasks:
            return "検索条件に一致するタスクが見つかりません"

        lines = [f"検索結果 ({len(all_tasks)}件):"]
        for task in all_tasks[:50]:
            lines.append(f"\n- {task['name']} (ID: {task['id']})")
            if task.get("list"):
                lines.append(f"  リスト: {task['list']['name']}")
            if task.get("status"):
                lines.append(f"  ステータス: {task['status']['status']}")
            if task.get("assignees"):
                names = [a.get("username", a.get("email", "不明")) for a in task["assignees"]]
                lines.append(f"  担当者: {', '.join(names)}")
            if task.get("tags"):
                tag_names = [t["name"] for t in task["tags"]]
                lines.append(f"  タグ: {', '.join(tag_names)}")
            if task.get("start_date"):
                try:
                    start = datetime.fromtimestamp(int(task["start_date"]) / 1000)
                    lines.append(f"  開始日: {start.strftime('%Y-%m-%d')}")
                except (ValueError, TypeError):
                    pass
            if task.get("due_date"):
                try:
                    due = datetime.fromtimestamp(int(task["due_date"]) / 1000)
                    lines.append(f"  期限: {due.strftime('%Y-%m-%d')}")
                except (ValueError, TypeError):
                    pass

        if len(all_tasks) > 50:
            lines.append(f"\n... 他 {len(all_tasks) - 50} 件のタスクがあります")

        return "\n".join(lines)

    @mcp.tool()
    async def get_weekly_summary() -> str:
        """データベースからタスクサマリーを取得します（バックグラウンド同期データ）。

        DBが利用できない場合はAPIから直接取得します。
        """
        # まずDBから試みる（高速）
        if db.enabled:
            summary = await asyncio.to_thread(db.get_task_summary)
            if summary:
                return summary

        # DBが使えない場合はAPIでフォールバック
        if not api.api_key:
            return "エラー: データベース・API共に利用できません"

        target_team = api.team_id
        if not target_team:
            return "エラー: CLICKUP_TEAM_IDが設定されていません"

        resp = await api.get(
            f"/team/{target_team}/task",
            params={"team_id": target_team, "include_closed": "true"},
        )

        if resp.status_code != 200:
            return f"エラー: タスク取得に失敗しました (ステータスコード: {resp.status_code})"

        tasks_data = resp.json().get("tasks", [])
        if not tasks_data:
            return "タスクが見つかりません"

        # ステータス別集計
        status_counts: dict[str, int] = {}
        for task in tasks_data:
            status = task.get("status", {}).get("status", "不明")
            status_counts[status] = status_counts.get(status, 0) + 1

        lines = [f"現在のタスク状況 (合計: {len(tasks_data)}件)"]
        for status, count in sorted(status_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {status}: {count}件")

        lines.append(f"\n(APIから直接取得 — DBキャッシュは利用不可)")
        return "\n".join(lines)
