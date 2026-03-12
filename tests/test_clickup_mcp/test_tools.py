"""ツールモジュールのテスト: 各ツールの出力フォーマットを検証。"""

import os
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest

from src.tools.external.clickup_mcp.api_client import ClickUpAPIClient
from src.tools.external.clickup_mcp.db_client import ClickUpDBClient


def _make_mock_response(status_code: int, json_data: dict) -> MagicMock:
    """httpx.Response のモックを作成する。"""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
    return resp


def _make_api_client(**env_overrides) -> ClickUpAPIClient:
    """テスト用 API クライアントを作成する。"""
    defaults = {
        "CLICKUP_API_KEY": "pk_test",
        "CLICKUP_TEAM_ID": "team_123",
        "CLICKUP_DEFAULT_LIST_ID": "list_456",
    }
    defaults.update(env_overrides)
    with patch.dict(os.environ, defaults):
        return ClickUpAPIClient()


def _make_db_client(enabled: bool = False) -> ClickUpDBClient:
    """テスト用 DB クライアントを作成する。"""
    with patch.dict(os.environ, {}, clear=True):
        client = ClickUpDBClient()
    client.enabled = enabled
    return client


# --- workspace tools ---


class TestWorkspaceTools:
    """ワークスペースツールのテスト。"""

    @pytest.mark.asyncio
    async def test_get_teams_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import workspace
        workspace.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "teams": [
                {"name": "テストチーム", "id": "t1"},
                {"name": "開発チーム", "id": "t2"},
            ]
        }))

        # ツール関数を直接呼び出す
        result = await mcp._tool_manager._tools["get_teams"].fn()
        assert "テストチーム" in result
        assert "t1" in result
        assert "開発チーム" in result

    @pytest.mark.asyncio
    async def test_get_teams_no_api_key(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client(CLICKUP_API_KEY="")
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import workspace
        workspace.register(mcp, api, db)

        result = await mcp._tool_manager._tools["get_teams"].fn()
        assert "エラー" in result
        assert "CLICKUP_API_KEY" in result

    @pytest.mark.asyncio
    async def test_get_spaces_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import workspace
        workspace.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "spaces": [
                {"name": "プロジェクトA", "id": "s1", "statuses": [{"status": "Open"}]},
            ]
        }))

        result = await mcp._tool_manager._tools["get_spaces"].fn()
        assert "プロジェクトA" in result
        assert "s1" in result

    @pytest.mark.asyncio
    async def test_get_lists_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import workspace
        workspace.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "lists": [
                {"name": "Backlog", "id": "l1", "task_count": 10},
            ]
        }))

        result = await mcp._tool_manager._tools["get_lists"].fn(space_id="s1")
        assert "Backlog" in result
        assert "l1" in result
        assert "10" in result

    @pytest.mark.asyncio
    async def test_get_folders_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import workspace
        workspace.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "folders": [
                {"name": "開発フォルダー", "id": "f1", "lists": [{"id": "l1"}, {"id": "l2"}]},
                {"name": "運用フォルダー", "id": "f2", "lists": []},
            ]
        }))

        result = await mcp._tool_manager._tools["get_folders"].fn(space_id="s1")
        assert "開発フォルダー" in result
        assert "f1" in result
        assert "リスト数: 2" in result
        assert "運用フォルダー" in result

    @pytest.mark.asyncio
    async def test_get_folder_lists_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import workspace
        workspace.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "lists": [
                {"name": "Sprint 1", "id": "l1", "task_count": 5},
                {"name": "Sprint 2", "id": "l2", "task_count": 3},
            ]
        }))

        result = await mcp._tool_manager._tools["get_folder_lists"].fn(folder_id="f1")
        assert "Sprint 1" in result
        assert "l1" in result
        assert "5" in result
        assert "Sprint 2" in result
        assert "フォルダー f1" in result


# --- tasks tools ---


class TestTasksTools:
    """タスク管理ツールのテスト。"""

    @pytest.mark.asyncio
    async def test_create_task_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import tasks
        tasks.register(mcp, api, db)

        api.post = AsyncMock(return_value=_make_mock_response(200, {
            "id": "task_123",
            "name": "テストタスク",
            "url": "https://app.clickup.com/t/task_123",
            "status": {"status": "Open"},
        }))

        result = await mcp._tool_manager._tools["create_task"].fn(name="テストタスク")
        assert "テストタスク" in result
        assert "task_123" in result
        assert "https://app.clickup.com" in result

    @pytest.mark.asyncio
    async def test_create_task_with_dates(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import tasks
        tasks.register(mcp, api, db)

        api.post = AsyncMock(return_value=_make_mock_response(201, {
            "id": "task_456",
            "name": "日付付きタスク",
            "url": "https://app.clickup.com/t/task_456",
            "status": {"status": "Open"},
        }))

        result = await mcp._tool_manager._tools["create_task"].fn(
            name="日付付きタスク",
            due_date="2026-02-20",
            due_time="14:00",
            start_date="2026-02-20",
            start_time="13:00",
        )
        assert "日付付きタスク" in result

        # API に送られたデータに日時が含まれることを確認
        call_args = api.post.call_args
        json_data = call_args.kwargs.get("json") or call_args[1].get("json")
        assert "start_date" in json_data
        assert "due_date" in json_data
        assert json_data["start_date_time"] is True
        assert json_data["due_date_time"] is True

    @pytest.mark.asyncio
    async def test_get_tasks_pagination(self):
        """get_tasksの自動ページネーションを確認。"""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import tasks
        tasks.register(mcp, api, db)

        page0 = [{"id": f"t{i}", "name": f"タスク{i}", "status": {"status": "Open"}} for i in range(100)]
        page1 = [{"id": f"t{i}", "name": f"タスク{i}", "status": {"status": "Open"}} for i in range(100, 120)]

        api.get = AsyncMock(side_effect=[
            _make_mock_response(200, {"tasks": page0}),
            _make_mock_response(200, {"tasks": page1}),
        ])

        result = await mcp._tool_manager._tools["get_tasks"].fn()
        assert "120件" in result
        assert api.get.call_count == 2

    @pytest.mark.asyncio
    async def test_get_tasks_exclude_no_start_date(self):
        """get_tasksのexclude_no_start_dateフィルタを確認。"""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import tasks
        tasks.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "tasks": [
                {"id": "t1", "name": "開始日あり", "status": {"status": "Open"}, "start_date": "1708300800000"},
                {"id": "t2", "name": "開始日なし", "status": {"status": "Open"}, "start_date": None},
            ]
        }))

        result = await mcp._tool_manager._tools["get_tasks"].fn(exclude_no_start_date=True)
        assert "開始日あり" in result
        assert "開始日なし" not in result
        assert "1件" in result

    @pytest.mark.asyncio
    async def test_update_task_status_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import tasks
        tasks.register(mcp, api, db)

        api.put = AsyncMock(return_value=_make_mock_response(200, {
            "name": "テストタスク",
        }))

        result = await mcp._tool_manager._tools["update_task_status"].fn(
            task_id="t1", status="Done"
        )
        assert "Done" in result
        assert "テストタスク" in result

    @pytest.mark.asyncio
    async def test_delete_task_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import tasks
        tasks.register(mcp, api, db)

        api.delete = AsyncMock(return_value=_make_mock_response(200, {}))

        result = await mcp._tool_manager._tools["delete_task"].fn(task_id="t1")
        assert "削除" in result
        assert "t1" in result

    @pytest.mark.asyncio
    async def test_get_task_detail_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import tasks
        tasks.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "id": "t1",
            "name": "詳細テスト",
            "status": {"status": "In Progress"},
            "url": "https://app.clickup.com/t/t1",
            "description": "テスト説明文",
            "date_created": "1708300800000",
            "date_updated": "1708387200000",
            "custom_fields": [],
        }))

        result = await mcp._tool_manager._tools["get_task_detail"].fn(task_id="t1")
        assert "詳細テスト" in result
        assert "In Progress" in result


# --- search tools ---


class TestSearchTools:
    """検索ツールのテスト。"""

    @pytest.mark.asyncio
    async def test_search_tasks_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import search
        search.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "tasks": [
                {
                    "id": "t1",
                    "name": "検索結果タスク",
                    "status": {"status": "Open"},
                    "list": {"name": "Backlog"},
                },
            ]
        }))

        result = await mcp._tool_manager._tools["search_tasks"].fn(query="検索")
        assert "検索結果タスク" in result
        assert "1件" in result

    @pytest.mark.asyncio
    async def test_search_tasks_statuses_params(self):
        """statuses フィルタが statuses[] 形式で送信されることを検証。"""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import search
        search.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {"tasks": []}))

        await mcp._tool_manager._tools["search_tasks"].fn(
            statuses=["Open", "In Progress"]
        )

        call_args = api.get.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params")
        # params はタプルのリスト形式
        assert ("statuses[]", "Open") in params
        assert ("statuses[]", "In Progress") in params
        # カンマ区切りでないことを確認
        for key, val in params:
            assert "," not in val or key == "query"

    @pytest.mark.asyncio
    async def test_search_tasks_start_date_in_response(self):
        """レスポンスに start_date が含まれることを検証。"""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import search
        search.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "tasks": [
                {
                    "id": "t1",
                    "name": "日付テスト",
                    "status": {"status": "Open"},
                    "start_date": "1708300800000",
                    "due_date": "1708387200000",
                },
            ]
        }))

        result = await mcp._tool_manager._tools["search_tasks"].fn(query="日付")
        assert "開始日:" in result
        assert "期限:" in result

    @pytest.mark.asyncio
    async def test_search_tasks_start_date_filter(self):
        """start_date_gt / start_date_lt フィルタがパラメータに含まれることを検証。"""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import search
        search.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {"tasks": []}))

        await mcp._tool_manager._tools["search_tasks"].fn(
            start_date_gt="2026-01-01",
            start_date_lt="2026-12-31",
        )

        call_args = api.get.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params")
        param_keys = [k for k, _ in params]
        assert "start_date_gt" in param_keys
        assert "start_date_lt" in param_keys

    @pytest.mark.asyncio
    async def test_search_tasks_exclude_closed_default(self):
        """exclude_closed=True（デフォルト）でinclude_closedが送られないことを確認。"""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import search
        search.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "tasks": [
                {"id": "t1", "name": "オープンタスク", "status": {"status": "Open"}},
            ]
        }))

        result = await mcp._tool_manager._tools["search_tasks"].fn(query="テスト")
        assert "オープンタスク" in result

        # include_closedが送られていないことを確認（タプルリスト形式）
        call_params = api.get.call_args.kwargs.get("params") or api.get.call_args[1].get("params")
        param_keys = [k for k, _ in call_params]
        assert "include_closed" not in param_keys

    @pytest.mark.asyncio
    async def test_search_tasks_include_closed(self):
        """exclude_closed=Falseでinclude_closed=trueが送られることを確認。"""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import search
        search.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "tasks": [
                {"id": "t1", "name": "完了タスク", "status": {"status": "Closed", "type": "closed"}},
            ]
        }))

        result = await mcp._tool_manager._tools["search_tasks"].fn(
            query="テスト", exclude_closed=False
        )
        assert "完了タスク" in result

        call_params = api.get.call_args.kwargs.get("params") or api.get.call_args[1].get("params")
        assert ("include_closed", "true") in call_params

    @pytest.mark.asyncio
    async def test_search_tasks_exclude_no_start_date(self):
        """exclude_no_start_date=Trueで開始日なしタスクが除外されることを確認。"""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import search
        search.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "tasks": [
                {"id": "t1", "name": "開始日あり", "status": {"status": "Open"}, "start_date": "1708300800000"},
                {"id": "t2", "name": "開始日なし", "status": {"status": "Open"}, "start_date": None},
                {"id": "t3", "name": "開始日キーなし", "status": {"status": "Open"}},
            ]
        }))

        result = await mcp._tool_manager._tools["search_tasks"].fn(
            query="テスト", exclude_no_start_date=True
        )
        assert "開始日あり" in result
        assert "開始日なし" not in result
        assert "開始日キーなし" not in result
        assert "1件" in result

    @pytest.mark.asyncio
    async def test_search_tasks_pagination(self):
        """100件以上のタスクがある場合にページネーションされることを確認。"""
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import search
        search.register(mcp, api, db)

        page0_tasks = [{"id": f"t{i}", "name": f"タスク{i}", "status": {"status": "Open"}} for i in range(100)]
        page1_tasks = [{"id": f"t{i}", "name": f"タスク{i}", "status": {"status": "Open"}} for i in range(100, 130)]

        api.get = AsyncMock(side_effect=[
            _make_mock_response(200, {"tasks": page0_tasks}),
            _make_mock_response(200, {"tasks": page1_tasks}),
        ])

        result = await mcp._tool_manager._tools["search_tasks"].fn(query="タスク")
        assert "130件" in result
        # 2回API呼び出しされたことを確認
        assert api.get.call_count == 2

    @pytest.mark.asyncio
    async def test_get_weekly_summary_from_db(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client(enabled=True)

        from src.tools.external.clickup_mcp.tools import search
        search.register(mcp, api, db)

        db.get_task_summary = MagicMock(return_value="現在のタスク状況 (合計: 5件)\n- Open: 3件\n- Done: 2件")

        result = await mcp._tool_manager._tools["get_weekly_summary"].fn()
        assert "合計: 5件" in result
        assert "Open: 3件" in result

    @pytest.mark.asyncio
    async def test_get_weekly_summary_api_fallback(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client(enabled=False)

        from src.tools.external.clickup_mcp.tools import search
        search.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "tasks": [
                {"status": {"status": "Open"}},
                {"status": {"status": "Open"}},
                {"status": {"status": "Done"}},
            ]
        }))

        result = await mcp._tool_manager._tools["get_weekly_summary"].fn()
        assert "合計: 3件" in result
        assert "APIから直接取得" in result


# --- comments tools ---


class TestCommentsTools:
    """コメントツールのテスト。"""

    @pytest.mark.asyncio
    async def test_add_comment_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import comments
        comments.register(mcp, api, db)

        api.post = AsyncMock(return_value=_make_mock_response(200, {}))

        result = await mcp._tool_manager._tools["add_comment"].fn(
            task_id="t1", comment_text="テストコメント"
        )
        assert "コメントを追加しました" in result

    @pytest.mark.asyncio
    async def test_add_comment_empty_text(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import comments
        comments.register(mcp, api, db)

        result = await mcp._tool_manager._tools["add_comment"].fn(
            task_id="t1", comment_text="  "
        )
        assert "エラー" in result
        assert "空" in result

    @pytest.mark.asyncio
    async def test_get_task_comments_success(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("test")
        api = _make_api_client()
        db = _make_db_client()

        from src.tools.external.clickup_mcp.tools import comments
        comments.register(mcp, api, db)

        api.get = AsyncMock(return_value=_make_mock_response(200, {
            "comments": [
                {
                    "user": {"username": "testuser"},
                    "date": "1708300800000",
                    "comment_text": "テストコメント内容",
                },
            ]
        }))

        result = await mcp._tool_manager._tools["get_task_comments"].fn(task_id="t1")
        assert "testuser" in result
        assert "テストコメント内容" in result
