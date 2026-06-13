"""画像プロンプトビルダー

会話コンテキストからDanbooruスタイルのタグを生成し、
キャラクターの外見タグと組み合わせて画像生成用プロンプトを構築する。
"""

from __future__ import annotations

import logging
import os
from typing import List, Tuple

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────
# デフォルトネガティブプロンプト
# ────────────────────────────────────────────
_DEFAULT_NEGATIVE = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, "
    "extra digit, fewer digits, cropped, worst quality, low quality, "
    "normal quality, jpeg artifacts, signature, watermark, username, blurry"
)

# ────────────────────────────────────────────
# LLM 用システムプロンプト
# ────────────────────────────────────────────
_SYSTEM_PROMPT = """\
あなたは会話テキストからアニメ画像生成用のDanbooruタグを生成する専門家です。

与えられた会話ログを読み、現在のシーンの状況・表情・感情・ポーズ・背景を推測し、
Danbooruスタイルのタグをカンマ区切りで出力してください。

ルール:
- タグのみをカンマ区切りで出力する（説明文や前置きは不要）
- 英語のDanbooruタグを使用する
- キャラクターの外見（髪色・目色等）は含めない（別途指定される）
- シーン描写に集中する: 表情、ポーズ、背景、雰囲気、アクション
- 15〜25タグ程度に収める
- クオリティタグ（masterpiece, best quality等）は含めない

出力例:
smile, looking at viewer, sitting, indoors, classroom, desk, window, sunlight, happy, relaxed, school uniform
"""


async def build_image_prompt(
    conversation_messages: list,
    character_appearance_tags: str,
    scene_description: str = "",
) -> Tuple[str, str]:
    """会話コンテキストから画像生成プロンプトを構築する。

    Args:
        conversation_messages: 直近5〜10件のメッセージ (role, content)
        character_appearance_tags: キャラクターの固定外見タグ
        scene_description: 追加のシーン説明（任意）

    Returns:
        (positive_prompt, negative_prompt) のタプル
    """
    # フォールバック用
    fallback_positive = _build_positive(character_appearance_tags, scene_description)

    try:
        scene_tags = await _generate_scene_tags(conversation_messages, scene_description)
        positive = _build_positive(character_appearance_tags, scene_tags)
        return positive, _DEFAULT_NEGATIVE
    except Exception as e:
        logger.warning("シーンタグ生成に失敗しました。フォールバックを使用します: %s", e)
        return fallback_positive, _DEFAULT_NEGATIVE


def _build_positive(appearance_tags: str, scene_tags: str) -> str:
    """クオリティタグ + 外見タグ + シーンタグ を結合する。"""
    parts = ["masterpiece, best quality"]
    if appearance_tags and appearance_tags.strip():
        parts.append(appearance_tags.strip())
    if scene_tags and scene_tags.strip():
        parts.append(scene_tags.strip())
    return ", ".join(parts)


def _format_conversation(messages: list) -> str:
    """会話メッセージをLLM入力用テキストに変換する。"""
    lines = []
    for msg in messages[-10:]:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            lines.append(f"ユーザー: {content}")
        elif role == "assistant":
            lines.append(f"アシスタント: {content}")
        else:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def _generate_scene_tags(
    messages: list,
    scene_description: str = "",
) -> str:
    """Gemini APIを使用してシーンタグを生成する。"""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY / GOOGLE_API_KEY が設定されていません")
        return ""

    try:
        import google.generativeai as genai
    except ImportError:
        logger.warning("google-generativeai パッケージがインストールされていません")
        return ""

    genai.configure(api_key=api_key)

    # モデル選択: gemini-2.0-flash を優先
    model_name = "gemini-2.0-flash"
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=_SYSTEM_PROMPT,
    )

    # ユーザープロンプト構築
    conversation_text = _format_conversation(messages)
    user_prompt = f"以下の会話から現在のシーンのDanbooruタグを生成してください:\n\n{conversation_text}"
    if scene_description:
        user_prompt += f"\n\n追加のシーン情報: {scene_description}"

    response = await _call_gemini_async(model, user_prompt)

    # レスポンスからタグのみ抽出（余分な装飾を除去）
    tags = response.strip()
    # Markdownコードブロックが含まれる場合は除去
    if tags.startswith("```"):
        lines = tags.split("\n")
        tags = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()

    return tags


async def _call_gemini_async(model, prompt: str) -> str:
    """Gemini API を非同期で呼び出す。

    google.generativeai は同期APIのため、スレッドプールで実行する。
    """
    import asyncio

    def _sync_call():
        response = model.generate_content(prompt)
        return response.text

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_call)
