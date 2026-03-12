"""X検索ツール（投稿検索・ユーザー検索）"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..api_client import parse_handles, validate_iso_date, XSearchAPIClient

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

SEARCH_SYSTEM_PROMPT = (
    "あなたは速報性の高いニュースリサーチャーです。"
    "GrokのX検索ツールで取得した投稿について、以下の形式で報告してください:\n"
    "1. 主要な投稿の要点を箇条書きでまとめる（各投稿にポストURLを付記）\n"
    "2. 投稿者のハンドルとポストURLを必ず含める\n"
    "3. 投稿日時がわかる場合は記載する"
)

USER_SEARCH_SYSTEM_PROMPT = (
    "あなたはSNSアナリストです。"
    "指定されたユーザーのX投稿を分析し、以下の形式で報告してください:\n"
    "1. 各投稿の要点をまとめる（ポストURLを付記）\n"
    "2. 投稿の傾向やトピックを簡潔に説明する\n"
    "3. 投稿日時がわかる場合は記載する"
)


def register(mcp: FastMCP, api_client: XSearchAPIClient) -> None:
    """検索ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def search_posts(
        query: str,
        max_results: int = 8,
        allowed_x_handles: str = "",
        excluded_x_handles: str = "",
        from_date: str = "",
        to_date: str = "",
        freshness: str = "auto",
        search_mode: str = "auto",
        language: str = "ja",
        enable_image_understanding: bool = False,
        enable_video_understanding: bool = False,
        temperature: float = 0.2,
        max_output_tokens: int = 900,
    ) -> str:
        """X(旧Twitter)の投稿を検索します。Grok x_search経由で最新ポストを取得します。

        Args:
            query: 検索クエリ
            max_results: 最大結果数 (1-25)
            allowed_x_handles: 検索対象ハンドル（カンマ区切り、例: "elonmusk,OpenAI"）
            excluded_x_handles: 除外ハンドル（カンマ区切り）
            from_date: 開始日 (YYYY-MM-DD)
            to_date: 終了日 (YYYY-MM-DD)
            freshness: 鮮度フィルタ (auto/day/week/month)
            search_mode: 検索モード (auto/latest/popular)
            language: 言語コード (ja/en等)
            enable_image_understanding: 画像理解を有効化
            enable_video_understanding: 動画理解を有効化
            temperature: 生成温度 (0.0-1.0)
            max_output_tokens: 最大出力トークン数
        """
        if not query or not query.strip():
            return "検索クエリを指定してください。"

        max_results = max(1, min(25, max_results))
        temperature = max(0.0, min(1.0, temperature))

        allowed = parse_handles(allowed_x_handles)
        excluded = parse_handles(excluded_x_handles)
        if allowed and excluded:
            return "allowed_x_handlesとexcluded_x_handlesは同時に指定できません。"

        try:
            parsed_from = validate_iso_date(from_date, "from_date")
            parsed_to = validate_iso_date(to_date, "to_date")
        except ValueError as exc:
            return str(exc)

        return await api_client.search(
            query,
            system_prompt=SEARCH_SYSTEM_PROMPT,
            max_results=max_results,
            allowed_handles=allowed or None,
            excluded_handles=excluded or None,
            from_date=parsed_from,
            to_date=parsed_to,
            freshness=freshness.lower() if freshness else "auto",
            search_mode=search_mode.lower() if search_mode else "auto",
            language=language,
            enable_image_understanding=enable_image_understanding,
            enable_video_understanding=enable_video_understanding,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

    @mcp.tool()
    async def search_user_posts(
        username: str,
        query: str = "",
        max_results: int = 10,
        from_date: str = "",
        to_date: str = "",
        freshness: str = "auto",
        search_mode: str = "latest",
        language: str = "ja",
        max_output_tokens: int = 900,
    ) -> str:
        """特定ユーザーのX投稿を検索します。

        Args:
            username: Xのユーザー名（@なしで指定、例: "elonmusk"）
            query: 追加の検索キーワード（省略可）
            max_results: 最大結果数 (1-25)
            from_date: 開始日 (YYYY-MM-DD)
            to_date: 終了日 (YYYY-MM-DD)
            freshness: 鮮度フィルタ (auto/day/week/month)
            search_mode: 検索モード (auto/latest/popular)
            language: 言語コード
            max_output_tokens: 最大出力トークン数
        """
        if not username or not username.strip():
            return "ユーザー名を指定してください。"

        handle = username.strip().lstrip("@")
        search_query = f"@{handle}の投稿"
        if query and query.strip():
            search_query = f"@{handle} {query.strip()}"

        max_results = max(1, min(25, max_results))

        try:
            parsed_from = validate_iso_date(from_date, "from_date")
            parsed_to = validate_iso_date(to_date, "to_date")
        except ValueError as exc:
            return str(exc)

        return await api_client.search(
            search_query,
            system_prompt=USER_SEARCH_SYSTEM_PROMPT,
            max_results=max_results,
            allowed_handles=[handle],
            from_date=parsed_from,
            to_date=parsed_to,
            freshness=freshness.lower() if freshness else "auto",
            search_mode=search_mode.lower() if search_mode else "latest",
            language=language,
            max_output_tokens=max_output_tokens,
        )
