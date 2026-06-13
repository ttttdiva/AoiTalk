"""
Web search tool for normal chat agents.

The backend can use either OpenAI's hosted WebSearchTool or the local
lightweight search service, depending on `search.provider`.
"""
import os
import asyncio
from ..core import tool as function_tool

from ..external_llm_permission import check_permission_sync
from ...services.quick_search_service import (
    SEARCH_PROVIDER_LOCAL,
    get_search_provider,
    local_web_search,
)


def _load_default_config():
    try:
        from ...config import Config

        return Config()
    except Exception:
        return None


def _run_async(coro_factory, timeout: int = 45):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(coro_factory()))
        return future.result(timeout=timeout)


def openai_web_search_impl(query: str) -> str:
    """OpenAI APIのHosted WebSearchToolで検索します。

    Args:
        query: 検索クエリ

    Returns:
        検索結果のサマリー
    """
    print(f"[Tool] web_search が呼び出されました: query='{query}'")

    try:
        # OpenAI APIキーを確認
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            return "Web検索を使用するにはOPENAI_API_KEYが必要です。"

        # OpenAI Agents SDKを使って検索を実行
        try:
            from agents import Agent, WebSearchTool, Runner

            agent = Agent(
                name="web-search-agent",
                model="gpt-4o",
                tools=[WebSearchTool()],
                instructions="あなたはWeb検索アシスタントです。与えられたクエリについて最新の情報を検索し、簡潔で正確な回答を日本語で提供してください。"
            )

            async def run_search():
                runner = Runner()
                return await runner.run(agent, f"以下について検索して教えてください：{query}")

            response = _run_async(run_search, timeout=45)

            if response and hasattr(response, 'text'):
                result = response.text
                print(f"[Tool] web_search 結果: {len(result)}文字")
                return result
            elif response:
                result = str(response)
                print(f"[Tool] web_search 結果: {len(result)}文字")
                return result
            else:
                return "検索結果を取得できませんでした。"

        except Exception as e:
            error_msg = f"OpenAI Web検索エラー: {str(e)}"
            print(f"[Tool] web_search エラー: {error_msg}")
            return error_msg

    except Exception as e:
        error_msg = f"Web検索エラー: {str(e)}"
        print(f"[Tool] web_search エラー: {error_msg}")
        return error_msg


def local_web_search_impl(query: str, config=None) -> str:
    """AoiTalk側の汎用Web検索で検索します。"""
    print(f"[Tool] local web_search が呼び出されました: query='{query}'")
    try:
        return local_web_search(query, config=config)
    except Exception as e:
        error_msg = f"汎用Web検索エラー: {str(e)}"
        print(f"[Tool] web_search エラー: {error_msg}")
        return error_msg


def web_search_impl(query: str, config=None) -> str:
    """設定された通常検索プロバイダでWeb検索を実行します。"""
    return web_search_with_config(query, config=config)


def web_search_with_config(query: str, config=None) -> str:
    """設定に応じてOpenAI Hosted Searchまたは汎用Web検索を実行します。"""
    active_config = config if config is not None else _load_default_config()
    provider = get_search_provider(active_config)

    if provider == SEARCH_PROVIDER_LOCAL:
        return local_web_search_impl(query, active_config)

    approved = check_permission_sync(
        tool_name="web_search",
        tool_args={"query": query},
        description=f"OpenAI APIによるWeb検索: 「{query}」",
    )

    if not approved:
        return "ユーザーによって検索がキャンセルされました。"

    return openai_web_search_impl(query)


def web_search_with_permission(query: str) -> str:
    """後方互換用: 設定された通常検索プロバイダで検索します。"""
    return web_search_with_config(query)


@function_tool
def web_search(query: str) -> str:
    """Web検索を実行します。

    Args:
        query: 検索クエリ

    Returns:
        検索結果のサマリー
    """
    return web_search_with_permission(query)

