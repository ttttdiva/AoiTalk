"""
Web search tool for Gemini models using OpenAI's gpt-4o as proxy
"""
import os
import asyncio
from ..core import tool as function_tool

from ..external_llm_permission import check_permission


def web_search_impl(query: str) -> str:
    """Web検索を実行します（純粋関数版）
    
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
        
        # OpenAI Agents SDKを使ってWeb検索を実行
        try:
            from agents import Agent, WebSearchTool, Runner
            
            # WebSearchTool付きのAgentを作成
            agent = Agent(
                name="web-search-agent",
                model="gpt-4o",
                tools=[WebSearchTool()],
                instructions="あなたはWeb検索アシスタントです。与えられたクエリについて最新の情報を検索し、簡潔で正確な回答を日本語で提供してください。"
            )
            
            # Runnerを使って検索を実行（非同期）
            async def run_search():
                runner = Runner()
                return await runner.run(agent, f"以下について検索して教えてください：{query}")
            
            # 同期実行
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # イベントループが既に実行中の場合
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, run_search())
                        response = future.result(timeout=30)
                else:
                    response = asyncio.run(run_search())
            except RuntimeError:
                # asyncio.runを使用
                response = asyncio.run(run_search())
            
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


async def web_search_with_permission(query: str) -> str:
    """Web検索を許可チェック付きで実行します
    
    Args:
        query: 検索クエリ
        
    Returns:
        検索結果のサマリー、または拒否時のメッセージ
    """
    # Check user permission
    approved = await check_permission(
        tool_name="web_search",
        tool_args={"query": query},
        description=f"Web検索: 「{query}」"
    )
    
    if not approved:
        return "ユーザーによって検索がキャンセルされました。"
    
    # Execute the actual search
    return web_search_impl(query)


@function_tool
def web_search(query: str) -> str:
    """Web検索を実行します（Gemini向けOpenAI proxy実装）
    
    Args:
        query: 検索クエリ
        
    Returns:
        検索結果のサマリー
    """
    # Check if we're in a running event loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Event loop is running - we're being called from async context (e.g., Gemini tool execution)
            # Permission check via WebSocket won't work from ThreadPoolExecutor, so skip it
            # and execute the search directly in a background thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(web_search_impl, query)
                return future.result(timeout=60)
        else:
            # No running loop - safe to use asyncio.run with permission check
            return asyncio.run(web_search_with_permission(query))
    except RuntimeError:
        # Fallback to sync version without permission check
        return web_search_impl(query)

