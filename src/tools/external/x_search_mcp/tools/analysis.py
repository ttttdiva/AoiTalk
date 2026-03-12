"""X トピック分析ツール"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..api_client import XSearchAPIClient

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

ANALYSIS_PROMPTS = {
    "summary": (
        "あなたはSNS分析の専門家です。"
        "指定されたトピックについてXの投稿を調査し、以下の形式で分析結果を報告してください:\n"
        "1. **概要**: トピックに関する全体的な状況（2-3文）\n"
        "2. **主要な意見・情報**: 重要な投稿を箇条書きで5-8件（ポストURLを付記）\n"
        "3. **まとめ**: 全体的な傾向や注目点"
    ),
    "sentiment": (
        "あなたはSNS感情分析の専門家です。"
        "指定されたトピックについてXの投稿を調査し、以下の形式で感情分析を報告してください:\n"
        "1. **全体的な感情傾向**: ポジティブ/ネガティブ/中立の割合感\n"
        "2. **肯定的な意見**: 代表的な投稿を3-5件（ポストURLを付記）\n"
        "3. **否定的な意見**: 代表的な投稿を3-5件（ポストURLを付記）\n"
        "4. **総評**: 賛否のバランスと主な論点"
    ),
    "timeline": (
        "あなたはニュースタイムライン作成の専門家です。"
        "指定されたトピックについてXの投稿を時系列で調査し、以下の形式で報告してください:\n"
        "1. 時系列順に主要な投稿・出来事を列挙する（日時とポストURLを付記）\n"
        "2. 各イベント間の関連性や因果関係を説明する\n"
        "3. 最新の状況をまとめる"
    ),
}


def register(mcp: FastMCP, api_client: XSearchAPIClient) -> None:
    """分析ツールを MCP サーバーに登録する。"""

    @mcp.tool()
    async def analyze_topic(
        topic: str,
        aspect: str = "summary",
        max_results: int = 15,
        from_date: str = "",
        to_date: str = "",
        freshness: str = "week",
        language: str = "ja",
        max_output_tokens: int = 1500,
    ) -> str:
        """X上でのトピックに対する反応・議論を分析します。

        Args:
            topic: 分析対象のトピック（例: "生成AI規制"）
            aspect: 分析の観点 (summary=要約, sentiment=感情分析, timeline=時系列)
            max_results: 最大結果数 (1-25)
            from_date: 開始日 (YYYY-MM-DD)
            to_date: 終了日 (YYYY-MM-DD)
            freshness: 鮮度フィルタ (auto/day/week/month)
            language: 言語コード
            max_output_tokens: 最大出力トークン数
        """
        if not topic or not topic.strip():
            return "分析対象のトピックを指定してください。"

        aspect = aspect.lower() if aspect else "summary"
        if aspect not in ANALYSIS_PROMPTS:
            return f"aspectは summary/sentiment/timeline のいずれかを指定してください（指定値: {aspect}）"

        max_results = max(1, min(25, max_results))
        system_prompt = ANALYSIS_PROMPTS[aspect]

        from ..api_client import validate_iso_date

        try:
            parsed_from = validate_iso_date(from_date, "from_date")
            parsed_to = validate_iso_date(to_date, "to_date")
        except ValueError as exc:
            return str(exc)

        return await api_client.search(
            topic.strip(),
            system_prompt=system_prompt,
            max_results=max_results,
            from_date=parsed_from,
            to_date=parsed_to,
            freshness=freshness.lower() if freshness else "week",
            search_mode="auto",
            language=language,
            temperature=0.3,
            max_output_tokens=max_output_tokens,
        )
