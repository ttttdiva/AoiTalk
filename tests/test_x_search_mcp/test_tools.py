"""X Search MCP ツールのユニットテスト"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.tools.external.x_search_mcp.api_client import XSearchAPIClient


@pytest.fixture
def mock_api_client():
    """モック化した XSearchAPIClient を返す"""
    client = MagicMock(spec=XSearchAPIClient)
    client.search = AsyncMock(return_value="検索結果テキスト")
    return client


@pytest.fixture
def mcp_server():
    """FastMCPモックサーバーを返す"""
    from mcp.server.fastmcp import FastMCP

    return FastMCP("test_x_search")


@pytest.fixture
def registered_tools(mcp_server, mock_api_client):
    """全ツールを登録して返す"""
    from src.tools.external.x_search_mcp.tools import search, analysis

    search.register(mcp_server, mock_api_client)
    analysis.register(mcp_server, mock_api_client)
    return mcp_server


class TestSearchPosts:
    @pytest.mark.asyncio
    async def test_empty_query(self, registered_tools, mock_api_client):
        from src.tools.external.x_search_mcp.tools.search import register

        # search_posts は空クエリでエラーを返す
        mcp = MagicMock()
        tools = {}

        def capture_tool():
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

        mcp.tool = capture_tool
        register(mcp, mock_api_client)

        result = await tools["search_posts"](query="")
        assert "検索クエリを指定してください" in result

    @pytest.mark.asyncio
    async def test_conflicting_handles(self, registered_tools, mock_api_client):
        mcp = MagicMock()
        tools = {}

        def capture_tool():
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

        mcp.tool = capture_tool
        from src.tools.external.x_search_mcp.tools.search import register
        register(mcp, mock_api_client)

        result = await tools["search_posts"](
            query="test",
            allowed_x_handles="user1",
            excluded_x_handles="user2",
        )
        assert "同時に指定できません" in result

    @pytest.mark.asyncio
    async def test_invalid_date(self, registered_tools, mock_api_client):
        mcp = MagicMock()
        tools = {}

        def capture_tool():
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

        mcp.tool = capture_tool
        from src.tools.external.x_search_mcp.tools.search import register
        register(mcp, mock_api_client)

        result = await tools["search_posts"](query="test", from_date="invalid")
        assert "YYYY-MM-DD" in result

    @pytest.mark.asyncio
    async def test_successful_search(self, registered_tools, mock_api_client):
        mcp = MagicMock()
        tools = {}

        def capture_tool():
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

        mcp.tool = capture_tool
        from src.tools.external.x_search_mcp.tools.search import register
        register(mcp, mock_api_client)

        result = await tools["search_posts"](query="生成AI")
        assert result == "検索結果テキスト"
        mock_api_client.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_max_results_clamped(self, registered_tools, mock_api_client):
        mcp = MagicMock()
        tools = {}

        def capture_tool():
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

        mcp.tool = capture_tool
        from src.tools.external.x_search_mcp.tools.search import register
        register(mcp, mock_api_client)

        await tools["search_posts"](query="test", max_results=100)
        call_kwargs = mock_api_client.search.call_args[1]
        assert call_kwargs["max_results"] == 25


class TestSearchUserPosts:
    @pytest.mark.asyncio
    async def test_empty_username(self, registered_tools, mock_api_client):
        mcp = MagicMock()
        tools = {}

        def capture_tool():
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

        mcp.tool = capture_tool
        from src.tools.external.x_search_mcp.tools.search import register
        register(mcp, mock_api_client)

        result = await tools["search_user_posts"](username="")
        assert "ユーザー名を指定してください" in result

    @pytest.mark.asyncio
    async def test_username_with_at(self, registered_tools, mock_api_client):
        mcp = MagicMock()
        tools = {}

        def capture_tool():
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

        mcp.tool = capture_tool
        from src.tools.external.x_search_mcp.tools.search import register
        register(mcp, mock_api_client)

        await tools["search_user_posts"](username="@elonmusk")
        call_kwargs = mock_api_client.search.call_args[1]
        assert call_kwargs["allowed_handles"] == ["elonmusk"]


class TestAnalyzeTopic:
    @pytest.mark.asyncio
    async def test_empty_topic(self, registered_tools, mock_api_client):
        mcp = MagicMock()
        tools = {}

        def capture_tool():
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

        mcp.tool = capture_tool
        from src.tools.external.x_search_mcp.tools.analysis import register
        register(mcp, mock_api_client)

        result = await tools["analyze_topic"](topic="")
        assert "トピックを指定してください" in result

    @pytest.mark.asyncio
    async def test_invalid_aspect(self, registered_tools, mock_api_client):
        mcp = MagicMock()
        tools = {}

        def capture_tool():
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

        mcp.tool = capture_tool
        from src.tools.external.x_search_mcp.tools.analysis import register
        register(mcp, mock_api_client)

        result = await tools["analyze_topic"](topic="AI", aspect="invalid")
        assert "summary/sentiment/timeline" in result

    @pytest.mark.asyncio
    async def test_successful_analysis(self, registered_tools, mock_api_client):
        mcp = MagicMock()
        tools = {}

        def capture_tool():
            def decorator(func):
                tools[func.__name__] = func
                return func
            return decorator

        mcp.tool = capture_tool
        from src.tools.external.x_search_mcp.tools.analysis import register
        register(mcp, mock_api_client)

        result = await tools["analyze_topic"](topic="生成AI規制", aspect="sentiment")
        assert result == "検索結果テキスト"
        mock_api_client.search.assert_called_once()
