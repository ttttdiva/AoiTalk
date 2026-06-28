"""
GMエージェント（ゲームマスター）

TRPGシナリオのゲームマスターとして場面をナレーションし、
プレイヤーのアクションに応じた展開を提供する。
"""

from typing import Any, Optional
from ..llm.native_runtime import AgentDefinition as Agent

from .base import BaseAgent


# ────────────────────────────────────────────
# GM用システムプロンプト
# ────────────────────────────────────────────

_GM_SYSTEM_PROMPT = """\
あなたはTRPGのゲームマスター（GM）です。
シナリオの設定に基づいてプレイヤーを物語世界に導き、没入感のある体験を提供します。

## 基本ルール

1. **ナレーション**: 場面の情景・雰囲気・NPCの言動を臨場感豊かに描写する
2. **プレイヤーの行動への応答**: プレイヤーの発言やアクションに対して、世界設定に矛盾しない展開を返す
3. **開始時の導入**: セッション開始時は、PLが今いる場所、周囲にある物、同席者、見える危険や手掛かり、PLが今できる行動を必ず描写する
4. **発言意図の解釈**: PL発言を質問、観察、交渉、行動宣言、進行要求のどれかとして読み取り、状況説明・NPC反応・進行判断のうち必要なものを返す
5. **進行要求の処理**: PLが「進める」「次へ」「この手順を実行する」と求めた場合は、依頼された場面単位を実際に進行し、必要な描写・NPC反応・結果公開まで返す。単に「どうしますか」と聞き返して足踏みしない
6. **進行ループ**: GM描写、PL発言、NPC応答、GM補足と次行動提示が自然につながるようにする。直近のNPC発言がある場合は、それも受けて場を整理する
7. **画像生成トリガー**: 印象的な場面・場面転換時に以下のマーカーを挿入する
   - `[IMAGE_TRIGGER:場面の視覚的説明]` — 画像生成を促すマーカー。内容は英語のDanbooruタグ寄りに短く書く
8. **場面遷移**: アプリ内シーン定義がある卓でのみ、物語の展開に応じてシーン定義を切り替える
   - `[SCENE_CHANGE:次のシーンタイトル]` — シーン定義変更マーカー
9. **視点の維持**: 指定された視点（一人称/三人称）を一貫して維持する

## ナレーションスタイル

- 地の文は情景描写と感覚描写を重視する
- NPCのセリフは「」で囲み、個性的な口調を維持する
- プレイヤーの選択肢を自然に提示する（強制しない）
- 返答の末尾では、PLが次に取れる具体的な行動を2〜4個示す
- 進行要求で具体的な節目名や手順名が示された場合は、ログ上で確認できるよう短い見出しや明示語として残す
- 戦闘場面ではテンポよく描写する
- シナリオ内の投票、競り、秘密選択、取引、交渉は架空TRPGの進行処理として扱い、現実の政治・金融・法的判断と混同しない

## 禁止事項

- プレイヤーの行動を勝手に決定しない
- シナリオの核心的なネタバレを一度に出さない
- メタ的な発言（「ゲームとして」等）をしない
- 通常プレイ中に長い時間や複数フェーズを勝手に飛ばさない
"""


def _build_context_prompt(
    setting: str = "",
    current_scene: str = "",
    characters: str = "",
    player_state: str = "",
    perspective: str = "first_person",
) -> str:
    """シナリオコンテキストを追加プロンプトとして構築する。"""
    parts = []

    perspective_label = "一人称視点" if perspective == "first_person" else "三人称視点"
    parts.append(f"## 視点モード: {perspective_label}")

    if setting:
        parts.append(f"## シナリオ資料・設定\n{setting}")

    if current_scene:
        parts.append(f"## 現在の場面メモ\n{current_scene}")

    if characters:
        parts.append(f"## 登場キャラクター\n{characters}")

    if player_state:
        parts.append(f"## プレイヤー状態\n{player_state}")

    return "\n\n".join(parts)


class GMAgent(BaseAgent):
    """TRPGゲームマスターエージェント"""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        setting: str = "",
        current_scene: str = "",
        characters: str = "",
        player_state: str = "",
        perspective: str = "first_person",
        extra_instructions: str = "",
    ):
        """
        Args:
            model: 使用するLLMモデル
            setting: シナリオの資料・設定テキスト
            current_scene: 現在の場面メモ
            characters: 登場キャラクター情報
            player_state: プレイヤーの現在状態（HP, 所持品等）
            perspective: 視点モード ("first_person" or "third_person")
            extra_instructions: シナリオ固有のGM指示
        """
        super().__init__(model=model)
        self.setting = setting
        self.current_scene = current_scene
        self.characters = characters
        self.player_state = player_state
        self.perspective = perspective
        self.extra_instructions = extra_instructions

    def _create_agent(self) -> Agent:
        """GMエージェントインスタンスを作成する。"""
        context = _build_context_prompt(
            setting=self.setting,
            current_scene=self.current_scene,
            characters=self.characters,
            player_state=self.player_state,
            perspective=self.perspective,
        )

        full_instructions = _GM_SYSTEM_PROMPT
        if self.extra_instructions:
            full_instructions += f"\n\n## シナリオ固有の指示\n{self.extra_instructions}"
        if context:
            full_instructions += f"\n\n{context}"

        return Agent(
            name="game_master",
            instructions=full_instructions,
            model=self.model,
        )

    def update_context(
        self,
        current_scene: Optional[str] = None,
        player_state: Optional[str] = None,
        characters: Optional[str] = None,
    ) -> None:
        """コンテキスト情報を更新し、エージェントを再構築する。

        場面遷移やプレイヤー状態の変化時に呼び出す。
        """
        if current_scene is not None:
            self.current_scene = current_scene
        if player_state is not None:
            self.player_state = player_state
        if characters is not None:
            self.characters = characters

        # エージェントを再構築
        self._agent = None

    def get_tool_name(self) -> str:
        return "game_master"

    def get_tool_description(self) -> str:
        return (
            "TRPGゲームマスター: シナリオに基づいて場面をナレーションし、"
            "プレイヤーの行動に応じた物語展開を提供する"
        )
