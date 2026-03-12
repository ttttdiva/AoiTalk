"""
タスク管理ツール: create_task, get_tasks, update_task_status,
                  update_task, delete_task, get_task_detail
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from ..api_client import ClickUpAPIClient
    from ..db_client import ClickUpDBClient

from ..api_client import parse_datetime_to_ms


def _format_task_detail(task: dict) -> str:
    """API レスポンスのタスクオブジェクトを整形文字列にする。"""
    lines = [
        f"- 名前: {task.get('name', '不明')}",
        f"  ID: {task.get('id', '不明')}",
    ]

    if task.get("status"):
        lines.append(f"  ステータス: {task['status'].get('status', '不明')}")

    if task.get("priority") and task["priority"].get("priority"):
        priority_map = {"1": "緊急", "2": "高", "3": "通常", "4": "低"}
        p = str(task["priority"]["priority"])
        lines.append(f"  優先度: {priority_map.get(p, p)}")

    if task.get("due_date"):
        try:
            due = datetime.fromtimestamp(int(task["due_date"]) / 1000)
            lines.append(f"  期限: {due.strftime('%Y-%m-%d %H:%M')}")
        except (ValueError, TypeError):
            pass

    if task.get("start_date"):
        try:
            start = datetime.fromtimestamp(int(task["start_date"]) / 1000)
            lines.append(f"  開始: {start.strftime('%Y-%m-%d %H:%M')}")
        except (ValueError, TypeError):
            pass

    if task.get("assignees"):
        names = [a.get("username", a.get("email", "不明")) for a in task["assignees"]]
        lines.append(f"  担当者: {', '.join(names)}")

    if task.get("tags"):
        tag_names = [t["name"] for t in task["tags"]]
        lines.append(f"  タグ: {', '.join(tag_names)}")

    if task.get("description"):
        desc = task["description"][:200]
        if len(task["description"]) > 200:
            desc += "..."
        lines.append(f"  説明: {desc}")

    if task.get("url"):
        lines.append(f"  URL: {task['url']}")

    if task.get("list"):
        lines.append(f"  リスト: {task['list'].get('name', '不明')}")

    return "\n".join(lines)


def register(mcp: FastMCP, api: ClickUpAPIClient, db: ClickUpDBClient):
    """タスク管理ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def create_task(
        name: str,
        list_id: Optional[str] = None,
        description: Optional[str] = None,
        assignees: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        priority: Optional[int] = None,
        due_date: Optional[str] = None,
        start_date: Optional[str] = None,
        due_time: Optional[str] = None,
        start_time: Optional[str] = None,
    ) -> str:
        """ClickUpでタスクを作成します。

        Args:
            name: タスク名
            list_id: リストID（省略時はデフォルトリスト）
            description: タスクの説明
            assignees: 担当者のユーザーID一覧
            tags: タグ一覧
            priority: 優先度 (1=緊急, 2=高, 3=通常, 4=低)
            due_date: 期限日 (YYYY-MM-DD)
            start_date: 開始日 (YYYY-MM-DD)
            due_time: 期限時刻 (HH:MM)
            start_time: 開始時刻 (HH:MM)
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        target_list = list_id or api.default_list_id

        task_data: dict = {"name": name}
        if description:
            task_data["description"] = description
        if assignees:
            task_data["assignees"] = assignees
        if tags:
            task_data["tags"] = tags
        if priority is not None:
            task_data["priority"] = priority

        # 開始日時
        ts, has_time = parse_datetime_to_ms(start_date, start_time)
        if ts is not None:
            task_data["start_date"] = ts
            task_data["start_date_time"] = has_time

        # 期限日時
        ts, has_time = parse_datetime_to_ms(due_date, due_time)
        if ts is not None:
            task_data["due_date"] = ts
            task_data["due_date_time"] = has_time

        resp = await api.post(f"/list/{target_list}/task", json=task_data)

        if resp.status_code in (200, 201):
            task = resp.json()
            result = f"タスクを作成しました:\n"
            result += f"- 名前: {task['name']}\n"
            result += f"- ID: {task['id']}\n"
            result += f"- URL: {task['url']}"
            if task.get("status"):
                result += f"\n- ステータス: {task['status']['status']}"
            return result
        else:
            err = resp.json().get("err", resp.text)
            return f"エラー: タスク作成に失敗しました (ステータス: {resp.status_code}) - {err}"

    @mcp.tool()
    async def get_tasks(
        list_id: Optional[str] = None,
        include_closed: bool = False,
        exclude_no_start_date: bool = False,
    ) -> str:
        """リスト内のタスク一覧を取得します。

        Args:
            list_id: リストID（省略時はデフォルトリスト）
            include_closed: 完了済みタスクも含めるか
            exclude_no_start_date: 開始日が未設定のタスクを除外する（デフォルト: False）
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        target_list = list_id or api.default_list_id
        params: dict = {"archived": "false"}
        if include_closed:
            params["include_closed"] = "true"

        # 自動ページネーション
        all_tasks: list[dict] = []
        max_pages = 10
        for page in range(max_pages):
            params["page"] = str(page)
            resp = await api.get(f"/list/{target_list}/task", params=params)
            if resp.status_code != 200:
                if page == 0:
                    return f"エラー: タスク取得に失敗しました (ステータスコード: {resp.status_code})"
                break
            page_tasks = resp.json().get("tasks", [])
            all_tasks.extend(page_tasks)
            if len(page_tasks) < 100:
                break

        # クライアント側フィルタ
        if exclude_no_start_date:
            all_tasks = [t for t in all_tasks if t.get("start_date")]

        if not all_tasks:
            return f"リスト {target_list} にタスクが見つかりません"

        lines = [f"リスト {target_list} のタスク ({len(all_tasks)}件):"]
        for task in all_tasks[:50]:
            lines.append("")
            lines.append(_format_task_detail(task))

        if len(all_tasks) > 50:
            lines.append(f"\n... 他 {len(all_tasks) - 50} 件のタスクがあります")

        return "\n".join(lines)

    @mcp.tool()
    async def update_task_status(task_id: str, status: str) -> str:
        """タスクのステータスを更新します。

        Args:
            task_id: タスクID
            status: 新しいステータス (例: "Open", "In Progress", "Done")
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        resp = await api.put(f"/task/{task_id}", json={"status": status})

        if resp.status_code == 200:
            task = resp.json()
            return f"タスク '{task['name']}' のステータスを '{status}' に更新しました"
        else:
            err = resp.json().get("err", resp.text)
            return f"エラー: ステータス更新に失敗しました - {err}"

    @mcp.tool()
    async def update_task(
        task_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[int] = None,
        due_date: Optional[str] = None,
        start_date: Optional[str] = None,
        due_time: Optional[str] = None,
        start_time: Optional[str] = None,
        assignees_add: Optional[List[str]] = None,
        assignees_remove: Optional[List[str]] = None,
    ) -> str:
        """タスクの各フィールドを更新します（汎用更新）。

        Args:
            task_id: タスクID
            name: 新しいタスク名
            description: 新しい説明
            status: 新しいステータス
            priority: 新しい優先度 (1=緊急, 2=高, 3=通常, 4=低)
            due_date: 新しい期限日 (YYYY-MM-DD)
            start_date: 新しい開始日 (YYYY-MM-DD)
            due_time: 新しい期限時刻 (HH:MM)
            start_time: 新しい開始時刻 (HH:MM)
            assignees_add: 追加する担当者IDリスト
            assignees_remove: 削除する担当者IDリスト
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        data: dict = {}
        if name is not None:
            data["name"] = name
        if description is not None:
            data["description"] = description
        if status is not None:
            data["status"] = status
        if priority is not None:
            data["priority"] = priority

        # 開始日時
        ts, has_time = parse_datetime_to_ms(start_date, start_time)
        if ts is not None:
            data["start_date"] = ts
            data["start_date_time"] = has_time

        # 期限日時
        ts, has_time = parse_datetime_to_ms(due_date, due_time)
        if ts is not None:
            data["due_date"] = ts
            data["due_date_time"] = has_time

        # 担当者の追加/削除
        if assignees_add or assignees_remove:
            assignees_data: dict = {}
            if assignees_add:
                assignees_data["add"] = assignees_add
            if assignees_remove:
                assignees_data["rem"] = assignees_remove
            data["assignees"] = assignees_data

        if not data:
            return "エラー: 更新するフィールドが指定されていません"

        resp = await api.put(f"/task/{task_id}", json=data)

        if resp.status_code == 200:
            task = resp.json()
            updated_fields = ", ".join(data.keys())
            return f"タスク '{task['name']}' を更新しました (変更: {updated_fields})"
        else:
            err = resp.json().get("err", resp.text)
            return f"エラー: タスク更新に失敗しました - {err}"

    @mcp.tool()
    async def delete_task(task_id: str) -> str:
        """タスクを削除します（この操作は取り消せません）。

        Args:
            task_id: 削除するタスクのID
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        resp = await api.delete(f"/task/{task_id}")

        if resp.status_code in (200, 204):
            return f"タスク {task_id} を削除しました"
        else:
            err = resp.text
            try:
                err = resp.json().get("err", err)
            except Exception:
                pass
            return f"エラー: タスク削除に失敗しました - {err}"

    @mcp.tool()
    async def get_task_detail(task_id: str) -> str:
        """タスクの詳細情報を取得します。

        Args:
            task_id: タスクID
        """
        if not api.api_key:
            return "エラー: CLICKUP_API_KEYが設定されていません"

        resp = await api.get(f"/task/{task_id}")

        if resp.status_code != 200:
            err = resp.text
            try:
                err = resp.json().get("err", err)
            except Exception:
                pass
            return f"エラー: タスク詳細取得に失敗しました - {err}"

        task = resp.json()
        lines = ["タスク詳細:"]
        lines.append(_format_task_detail(task))

        # 追加情報
        if task.get("custom_fields"):
            lines.append("\nカスタムフィールド:")
            for field in task["custom_fields"]:
                value = field.get("value", "未設定")
                lines.append(f"  - {field.get('name', '不明')}: {value}")

        if task.get("time_estimate"):
            hours = task["time_estimate"] / 3600000
            lines.append(f"\n見積もり時間: {hours:.1f}時間")

        if task.get("time_spent"):
            hours = task["time_spent"] / 3600000
            lines.append(f"実績時間: {hours:.1f}時間")

        if task.get("date_created"):
            try:
                created = datetime.fromtimestamp(int(task["date_created"]) / 1000)
                lines.append(f"\n作成日: {created.strftime('%Y-%m-%d %H:%M')}")
            except (ValueError, TypeError):
                pass

        if task.get("date_updated"):
            try:
                updated = datetime.fromtimestamp(int(task["date_updated"]) / 1000)
                lines.append(f"更新日: {updated.strftime('%Y-%m-%d %H:%M')}")
            except (ValueError, TypeError):
                pass

        return "\n".join(lines)
