"""グループチャット応答管理"""

import asyncio
import logging
from typing import List, Dict, Any, Optional

from ..config import Config
from .prompts import build_unified_instructions
from ..services.character_service import get_character_for_prompt, _run_sync

logger = logging.getLogger(__name__)


class GroupChatManager:
    """複数キャラクターの同時会話を管理する"""

    def __init__(self, config: Config, character_slugs: List[str]):
        self.config = config
        self.character_slugs = character_slugs
        self._character_cache = {}

    async def generate_responses(
        self,
        user_message: str,
        history: List[Dict[str, Any]],
        strategy: str = "round_robin",
    ) -> List[Dict[str, Any]]:
        """各キャラクターの応答を順番に生成する

        strategy: "round_robin" / "random"
        戻り値: [{"character_slug": "xxx", "character_name": "XXX", "content": "応答"}]
        """
        turn_order = self._get_turn_order(strategy)
        responses = []
        accumulated_context = list(history)

        for slug in turn_order:
            char_data = await self._get_character(slug)
            if not char_data:
                continue

            # このキャラ用のプロンプト構築
            prompt = self._build_character_prompt(
                char_data, accumulated_context, user_message, responses
            )

            # LLM呼び出し（native runtimeで直接API呼び出し）
            content = await self._call_llm(char_data, prompt)

            response = {
                "character_slug": slug,
                "character_name": char_data.get("name", slug),
                "content": content,
            }
            responses.append(response)
            # 次のキャラのコンテキストに含める
            accumulated_context.append(
                {
                    "role": "assistant",
                    "content": f"[{char_data.get('name', slug)}]: {content}",
                }
            )

        return responses

    def _get_turn_order(self, strategy: str) -> List[str]:
        if strategy == "random":
            import random

            order = list(self.character_slugs)
            random.shuffle(order)
            return order
        return list(self.character_slugs)  # round_robin

    async def _get_character(self, slug: str) -> Optional[Dict]:
        if slug not in self._character_cache:
            self._character_cache[slug] = await get_character_for_prompt(slug)
        return self._character_cache[slug]

    def _build_character_prompt(self, char_data, history, user_message, prev_responses):
        """グループチャット用のプロンプトを構築"""
        name = char_data.get("name", "")
        description = char_data.get("description", "")
        personality = char_data.get("personality_summary", "")
        scenario = char_data.get("scenario", "")
        example_messages = char_data.get("example_messages", "")
        system_prompt = char_data.get("system_prompt", "")

        sections = []
        sections.append(f"あなたは「{name}」として、グループチャットに参加しています。")
        sections.append(
            "他のキャラクターの発言も踏まえて、自分のキャラクターらしく応答してください。"
        )
        sections.append(
            "行動やナレーションは *アスタリスク* で囲み、台詞はそのまま記述してください。"
        )

        if description:
            sections.append(f"\n## キャラクター設定\n{description}")
        if personality:
            sections.append(f"\n## 性格\n{personality}")
        if scenario:
            sections.append(f"\n## シナリオ\n{scenario}")
        if example_messages:
            sections.append(f"\n## 会話例\n{example_messages}")
        if system_prompt:
            sections.append(f"\n## 追加指示\n{system_prompt}")

        # 他のキャラの直前の応答を含める
        if prev_responses:
            others = "\n".join(
                f"[{r['character_name']}]: {r['content']}" for r in prev_responses
            )
            sections.append(f"\n## 他のキャラクターの発言\n{others}")

        return "\n".join(sections)

    async def _call_llm(self, char_data, prompt):
        """LLM APIを直接呼び出して応答を生成"""
        try:
            from .native_runtime import AgentDefinition, run_native_agent_once

            model = char_data.get("model") or "gpt-4o-mini"
            agent = AgentDefinition(
                name=char_data.get("name", "Character"),
                instructions=prompt,
                model=model,
            )
            result = await run_native_agent_once(agent, "")
            return result.final_output or ""
        except Exception as e:
            logger.error(f"グループチャットLLM呼び出し失敗: {e}")
            return f"[{char_data.get('name', '???')}の応答生成に失敗しました]"
