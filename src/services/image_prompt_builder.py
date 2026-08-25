"""画像プロンプトビルダー

会話コンテキストからDanbooruスタイルのタグを生成し、
キャラクターの外見タグと組み合わせて画像生成用プロンプトを構築する。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from types import SimpleNamespace
from typing import List, Tuple

from ..llm.conversation_context import persist_usage_sync
from .outbound_privacy_service import (
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)
from .turn_context import get_turn_context

logger = logging.getLogger(__name__)


def persist_usage_sync(*args, **kwargs):
    """Lazy usage persistence keeps optional Gemini imports isolated."""

    from ..llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))

_RECORDED_GEMINI_RESPONSES: list[object] = []


def _usage_client(context=None):
    """Return a context carrying the current turn identity for usage rows."""

    if context is not None and (
        hasattr(context, "current_session_id")
        or hasattr(context, "current_project_id")
        or callable(getattr(context, "_get_session_user_id", None))
    ):
        return context
    try:
        from .turn_context import get_turn_context

        turn = get_turn_context()
    except Exception:
        turn = None

    def _value(name, default=None):
        if isinstance(context, Mapping):
            value = context.get(name)
            if value is not None:
                return value
        value = getattr(context, name, None)
        if value is not None:
            return value
        return getattr(turn, name, default) if turn is not None else default

    user_id = _value("user_id")
    return SimpleNamespace(
        current_session_id=_value("current_session_id", _value("session_id")),
        current_project_id=_value("current_project_id", _value("project_id")),
        character_name=_value("character_name"),
        _get_session_user_id=lambda: user_id,
    )


def _mark_response_recorded(response: object) -> bool:
    try:
        if getattr(response, "_aoitalk_usage_recorded", False):
            return True
        object.__setattr__(response, "_aoitalk_usage_recorded", True)
        return False
    except Exception:
        if any(item is response for item in _RECORDED_GEMINI_RESPONSES):
            return True
        _RECORDED_GEMINI_RESPONSES.append(response)
        del _RECORDED_GEMINI_RESPONSES[:-16]
        return False


def _gemini_usage_payload(response):
    """Map Gemini SDK usage_metadata without estimating absent token counts."""

    metadata = getattr(response, "usage_metadata", None)
    if metadata is None and isinstance(response, Mapping):
        metadata = response.get("usage_metadata")
    if metadata is None:
        return None

    def _field(name):
        return metadata.get(name) if isinstance(metadata, Mapping) else getattr(metadata, name, None)

    def _count(name):
        value = _field(name)
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    input_tokens = _count("prompt_token_count")
    output_tokens = _count("candidates_token_count")
    if input_tokens is None and output_tokens is None:
        return None
    cached_tokens = _count("cached_content_token_count") or 0
    payload = {
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
        "cached_tokens": cached_tokens,
        "cache_read_tokens": cached_tokens,
        "reasoning_tokens": _count("thoughts_token_count") or 0,
        "cache_provider": "gemini",
        "metrics_source": "gemini.usage_metadata",
    }
    resolved_model = getattr(response, "model_version", None)
    if resolved_model is None and isinstance(response, Mapping):
        resolved_model = response.get("model_version")
    if resolved_model:
        payload["resolved_model"] = str(resolved_model)
    return payload


def _record_gemini_usage(
    response,
    *,
    model: str = "gemini-2.0-flash",
    started: float | None = None,
    latency_ms: int | None = None,
    usage_context=None,
) -> bool:
    payload = _gemini_usage_payload(response)
    if not payload or _mark_response_recorded(response):
        return False
    try:
        persist_usage_sync(
            _usage_client(usage_context),
            provider="gemini",
            model=model,
            usage=payload,
            request_type="vision",
            latency_ms=(
                max(0, int(latency_ms))
                if latency_ms is not None
                else (
                    max(0, int((time.monotonic() - started) * 1000))
                    if started is not None
                    else 0
                )
            ),
            is_streaming=False,
        )
        return True
    except Exception:
        logger.debug("画像プロンプト生成のGemini usage記録に失敗しました", exc_info=True)
        return False


def _record_image_usage(
    response,
    model: str = "gemini-2.0-flash",
    latency_ms: int = 0,
    usage_context=None,
) -> bool:
    """Compatibility alias for callers that label this as image usage."""

    return _record_gemini_usage(
        response,
        model=model,
        latency_ms=latency_ms,
        usage_context=usage_context,
    )

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
    usage_context=None,
    *,
    roleplay_pov: bool = False,
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
    fallback_positive = _build_positive(
        character_appearance_tags,
        scene_description,
        roleplay_pov=roleplay_pov,
    )

    try:
        scene_tags = await _generate_scene_tags(
            conversation_messages,
            scene_description,
            usage_context=usage_context,
        )
        positive = _build_positive(
            character_appearance_tags,
            scene_tags,
            roleplay_pov=roleplay_pov,
        )
        return positive, _DEFAULT_NEGATIVE
    except Exception as e:
        logger.warning("シーンタグ生成に失敗しました。フォールバックを使用します: %s", e)
        return fallback_positive, _DEFAULT_NEGATIVE


def _build_positive(
    appearance_tags: str,
    scene_tags: str,
    *,
    roleplay_pov: bool = False,
) -> str:
    """クオリティタグ + 外見タグ + シーンタグ を結合する。"""
    parts = ["masterpiece, best quality"]
    if roleplay_pov:
        parts.append(
            "pov, first-person view, looking at viewer, facing viewer, "
            "character in front of viewer, immersive roleplay scene"
        )
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
    usage_context=None,
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

    response = await _call_gemini_async(
        model,
        user_prompt,
        model_name=model_name,
        usage_context=usage_context,
    )

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


async def _call_gemini_async(
    model,
    prompt: str,
    *,
    model_name: str = "gemini-2.0-flash",
    usage_context=None,
) -> str:
    """Gemini API を非同期で呼び出す。

    google.generativeai は同期APIのため、スレッドプールで実行する。
    """
    import asyncio

    started = time.monotonic()
    # Image prompt generation is an external Gemini transport even when it is
    # called from a background/image-only path.  Resolve explicit request
    # config and inherited turn scope; never silently construct a None-config
    # gateway that defaults to direct mode.
    inherited = get_privacy_policy_context()

    def _value(name: str, *aliases: str):
        keys = (name, *aliases)
        if isinstance(usage_context, Mapping):
            for key in keys:
                value = usage_context.get(key)
                if value is not None:
                    return value
        for key in keys:
            value = getattr(usage_context, key, None)
            if value is not None:
                return value
        return None

    privacy_config = _value("config", "privacy_config")
    if privacy_config is None:
        try:
            from ..config import Config

            privacy_config = Config()
        except Exception as exc:
            raise PrivacyError(
                "画像プロンプトのプライバシー設定を解決できないため生成を停止しました"
            ) from exc
    session_context = _value("session_context", "privacy_context")
    project_metadata = _value("project_metadata", "project_context")
    if not isinstance(session_context, Mapping):
        session_context = inherited.session_context
    if not isinstance(project_metadata, Mapping):
        project_metadata = inherited.project_metadata
    try:
        turn = get_turn_context()
    except Exception:
        turn = None
    user_id = _value("user_id", "session_user_id") or getattr(turn, "user_id", None)
    session_id = _value("current_session_id", "session_id") or getattr(
        turn, "session_id", None
    )
    gateway = OutboundPrivacyGateway(
        privacy_config,
        user_id=str(user_id or ""),
        session_id=str(session_id or ""),
        session_context=session_context if isinstance(session_context, Mapping) else None,
        project_metadata=project_metadata if isinstance(project_metadata, Mapping) else None,
    )

    def _sync_call():
        protected = gateway.protect_sync(
            {"prompt": prompt},
            provider="gemini",
            source_kind="image_prompt",
        )
        response = model.generate_content(
            str((protected.payload or {}).get("prompt") or "")
        )
        _record_gemini_usage(
            response,
            model=model_name,
            started=started,
            usage_context=usage_context,
        )
        return gateway.restore(str(response.text or ""))

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_call)
