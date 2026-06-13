"""
インポートエージェント（ImportAgent）

シナリオ素材のインポートを支援する専門エージェント。
ディレクトリ分析、キャラクター/世界設定/シーンの柔軟な取り込みを行う。
"""

from agents import Agent

from ..tools.adapters import OpenAIAgentAdapter
from ..tools.import_tools import (
    analyze_import_files,
    import_file_as_character,
    import_file_as_lore,
    import_file_as_scene,
)
from .base import BaseAgent

_IMPORT_SYSTEM_PROMPT = """\
あなたはシナリオ素材のインポートを支援するエージェントです。

ユーザーが指定したディレクトリやファイルから、キャラクター設定・世界設定・シーンを
AoiTalkのシナリオデータベースに取り込みます。

## ワークフロー

1. ユーザーがディレクトリパスを指定したら、`analyze_import_files` で構造を分析
2. 分析結果をユーザーに提示し、各ファイルのインポート方法を確認
3. 確認後、ファイルごとに適切なインポートツールを呼び出す:
   - キャラクター設定 → `import_file_as_character`
   - 世界設定 → `import_file_as_lore`
   - シーン/エピソード → `import_file_as_scene`
4. インポート結果をまとめて報告

## 注意
- SillyTavern形式のPNGファイル（Character Card V2）が含まれる場合は、
  既存のCharacter Card V2インポート機能を案内する
- JSONファイルはWorldBook形式の可能性があるので確認する
- 不明なファイルはスキップするか、ユーザーに確認する
- ファイル内容のプレビューを見て、推定カテゴリが正しいか判断する
- 1つのファイルに複数カテゴリの情報が含まれる場合はユーザーに確認する
"""


class ImportAgent(BaseAgent):
    """シナリオ素材のインポート支援エージェント"""

    def __init__(self, model: str = "gpt-4o-mini"):
        """
        Args:
            model: 使用するLLMモデル
        """
        super().__init__(model=model)

    def _create_agent(self) -> Agent:
        """ImportAgentインスタンスを作成する。"""
        tools = OpenAIAgentAdapter.convert_all(
            [
                analyze_import_files,
                import_file_as_character,
                import_file_as_lore,
                import_file_as_scene,
            ]
        )

        return Agent(
            name="ImportAssistant",
            model=self.model,
            instructions=_IMPORT_SYSTEM_PROMPT,
            tools=tools,
        )

    def get_tool_name(self) -> str:
        return "import_assistant"

    def get_tool_description(self) -> str:
        return (
            "シナリオ素材のインポート: ディレクトリ分析、"
            "キャラ/世界設定/シーンの柔軟な取り込み"
        )
