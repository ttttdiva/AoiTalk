"""
ストーリークリエイターエージェント

ユーザーとの対話を通じてTRPGシナリオを構築する。
ジャンル・設定・キャラクター・シーンを対話的に決定し、
構造化JSONとして出力する。
"""

from typing import Any, Optional
from agents import Agent, ModelSettings

from .base import BaseAgent
from ..tools.adapters import OpenAIAgentAdapter
from ..tools.scenario_tools import get_coc_creation_reference_context


# ────────────────────────────────────────────
# ストーリークリエイター用システムプロンプト
# ────────────────────────────────────────────

_STORY_CREATOR_SYSTEM_PROMPT = """\
あなたはTRPGシナリオのクリエイターアシスタントです。
ユーザーと対話しながら、魅力的なインタラクティブストーリーのシナリオを一緒に作り上げます。

## 対話フロー

1. **ジャンル・テーマの確認**: ファンタジー、SF、ホラー、現代劇、歴史物など
2. **舞台設定の構築**: 世界観、時代、場所、雰囲気
3. **キャラクター設定**: 主要NPC（味方、敵、中立）の名前・性格・外見
4. **シーン構成**: 物語の流れ（オープニング→展開→クライマックス→エンディング）
5. **最終出力**: 構造化されたJSONデータ

## 出力形式

シナリオが完成したら、以下のJSON形式で出力してください:

```json
{
  "title": "シナリオタイトル",
  "description": "シナリオの概要説明",
  "genre": "ジャンル",
  "perspective": "first_person または third_person",
  "setting": "世界観・舞台設定の詳細",
  "opening_text": "冒頭ナレーション",
  "gm_instructions": "GMへの指示・注意事項",
  "tags": ["タグ1", "タグ2"],
  "characters": [
    {
      "name": "キャラクター名",
      "role": "npc / ally / enemy / narrator",
      "description": "キャラクター説明",
      "personality_override": "性格・口調の設定",
      "appearance_tags_override": "外見のDanbooruタグ"
    }
  ],
  "scenes": [
    {
      "title": "シーンタイトル",
      "description": "シーン説明",
      "scene_type": "normal / combat / dialogue / cutscene",
      "gm_instructions": "このシーンでのGM指示",
      "image_prompt": "シーン画像のプロンプト",
      "transitions": [
        {"condition": "遷移条件", "target_scene_title": "次のシーン名"}
      ]
    }
  ]
}
```

## ガイドライン

- ユーザーの要望を丁寧にヒアリングする
- 具体的な提案を出しつつ、ユーザーの意見を尊重する
- キャラクターには個性的な特徴を付ける
- シーンは5〜10程度を目安にする
- 各シーンに分岐の可能性を含める
- 画像プロンプトはDanbooruタグ形式で書く
- 出力JSONは上記フォーマットに厳密に従う
- JSON出力時は ```json ブロックで囲む
"""


class StoryCreatorAgent(BaseAgent):
    """シナリオ作成支援エージェント"""

    def __init__(self, model: str = "gpt-4o-mini"):
        super().__init__(model=model)

    def _create_agent(self) -> Agent:
        """ストーリークリエイターエージェントインスタンスを作成する。"""
        tools = OpenAIAgentAdapter.convert_all([get_coc_creation_reference_context])
        return Agent(
            name="story_creator",
            instructions=(
                _STORY_CREATOR_SYSTEM_PROMPT
                + "\n\nCoC6/CoC7シナリオ作成時は、必要に応じて "
                + "`get_coc_creation_reference_context` で構造化DBから関連ルール/神話生物だけを参照してください。"
                + "ルールブック全文やサプリ全文は要求・引用せず、取得した短い参照項目を設計判断の根拠にします。"
            ),
            model=self.model,
            model_settings=ModelSettings(tool_choice="auto"),
            tools=tools,
        )

    def get_tool_name(self) -> str:
        return "story_creator"

    def get_tool_description(self) -> str:
        return (
            "シナリオ作成アシスタント: ユーザーとの対話を通じてTRPGシナリオを構築し、"
            "構造化されたJSON形式で出力する"
        )
