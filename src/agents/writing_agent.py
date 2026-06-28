"""
執筆支援エージェント（WritingAgent）

小説・TRPGシナリオの執筆を支援する専門エージェント。
コンテキスト取得、本文生成、保存、Canon更新を行う。
"""

from ..llm.native_runtime import AgentDefinition as Agent

from ..tools.core import ensure_tool_definitions
from ..tools.writing_tools import (
    get_writing_context,
    save_scene_draft,
    update_canon_from_content,
    get_character_voice,
)
from .base import BaseAgent

_WRITING_SYSTEM_PROMPT = """\
あなたは小説・TRPGシナリオの執筆を支援する専門エージェントです。

## 基本ルール

1. **コンテキスト確認**: 執筆開始前に必ず `get_writing_context` を呼び出して、
   シナリオ全体・登場キャラ・Canon・文体定義・対象シーンがあれば前後関係を確認する。

2. **文体の維持**: voice定義（トーン、時制ルール、語彙レベル）に厳密に従う。
   禁止表現リストに含まれる表現は絶対に使わない。

3. **キャラクター一貫性**: 各キャラクターの口調パターンと会話例に基づいて、
   セリフと行動を描写する。不明な場合は `get_character_voice` で確認する。

4. **連続性**: 前のシーンの末尾からシームレスに繋がるように書く。
   次のシーンの概要を意識して、適切なフックを残す。

5. **ビートシートの遵守**: エピソードのビートシートが定義されている場合、
   各ビートを順序通りにカバーする。

6. **保存**: 対象シーンがある執筆セッションで本文が完成したら `save_scene_draft` で保存する。
   シナリオ全体の相談や設定案は、ユーザーに確認してからCanon更新や次の編集提案に進む。

7. **Canon更新**: 執筆中に新しい確定事実（地名、人物関係、出来事）が確立されたら、
   `update_canon_from_content` で記録する。

## 執筆スタイルガイドライン

- 地の文と台詞を適切に混ぜる
- 感覚描写（視覚以外も）を重視する
- 「Show, don't tell」— 感情は行動や反応で示す
- 文の長さにバリエーションをつける
- AI臭い表現（「確かに」「実に」「まさに」の多用、三つ組リスト）を避ける

## ユーザーとの対話

ユーザーが「このシナリオを考えて」「設定を作って」「構成を考えて」と言ったら:
1. get_writing_context でシナリオ全体のコンテキスト取得
2. 必要な設定・展開・キャラクター案を提案
3. 確定した事実はユーザー確認後に update_canon_from_content で保存

ユーザーが「このシーンを書いて」と言ったら:
1. get_writing_context でコンテキスト取得
2. コンテキストを確認し、不明点があればユーザーに質問
3. シーンを執筆
4. 対象シーンが設定されている場合は save_scene_draft で保存
5. 新しい確定事実があれば update_canon_from_content

ユーザーが「続きを書いて」と言ったら:
1. get_writing_context で現在のcontentを確認
2. 既存contentの続きから書く
3. 完了したら保存
"""


class WritingAgent(BaseAgent):
    """小説・シナリオ執筆支援エージェント"""

    def __init__(
        self,
        model: str = "gpt-4o",
        scenario_context: str = "",
        voice_definition: str = "",
    ):
        """
        Args:
            model: 使用するLLMモデル
            scenario_context: シナリオのコンテキスト情報（オプション）
            voice_definition: 文体定義（オプション）
        """
        super().__init__(model=model)
        self.scenario_context = scenario_context
        self.voice_definition = voice_definition

    def _create_agent(self) -> Agent:
        """WritingAgentインスタンスを作成する。"""
        tools = ensure_tool_definitions(
            [
                get_writing_context,
                save_scene_draft,
                update_canon_from_content,
                get_character_voice,
            ]
        )

        instructions = _WRITING_SYSTEM_PROMPT

        # コンテキスト情報があれば追加
        if self.scenario_context:
            instructions += f"\n\n## シナリオコンテキスト\n{self.scenario_context}"
        if self.voice_definition:
            instructions += f"\n\n## 文体定義\n{self.voice_definition}"

        return Agent(
            name="WritingAssistant",
            model=self.model,
            instructions=instructions,
            tools=tools,
        )

    def get_tool_name(self) -> str:
        return "writing_assistant"

    def get_tool_description(self) -> str:
        return "小説・シナリオの執筆支援: コンテキスト取得、本文生成、保存、Canon更新"
