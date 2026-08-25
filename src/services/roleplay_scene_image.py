"""Roleplay 応答の SCENE_DESCRIPTION 処理と画像生成。"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_SCENE_RE = re.compile(r"\[SCENE_DESCRIPTION:\s*(.+?)\]", re.DOTALL)
_STRIP_RE = re.compile(r"\n?\[SCENE_DESCRIPTION:\s*.+?\]\s*", re.DOTALL)


def extract_scene_description(response: str) -> str | None:
    match = _SCENE_RE.search(response or "")
    if not match:
        return None
    scene = match.group(1).strip()
    return scene or None


def strip_scene_description_markers(response: str) -> str:
    return _STRIP_RE.sub("", response or "").strip()


async def finalize_roleplay_assistant_response(
    client: Any,
    response_text: str,
    *,
    message_id: str | None = None,
    history: list[Any] | None = None,
    stream_callback: Any = None,
) -> str:
    """SCENE_DESCRIPTION を除去し、条件を満たせば ComfyUI 画像タグを付与する。"""
    scene_description = extract_scene_description(response_text)
    if not scene_description:
        return response_text

    visible = strip_scene_description_markers(response_text)
    assistant_message_id = message_id or str(uuid.uuid4())

    try:
        from .character_service import get_character_for_prompt
        from .generated_media_service import (
            generate_roleplay_scene_media,
            should_attempt_roleplay_generation,
        )
        from .image_prompt_builder import build_image_prompt

        char_data = await get_character_for_prompt(getattr(client, "character_name", ""))
        session_id = str(getattr(client, "current_session_id", "") or "")
        owner_user_id = str(getattr(client, "session_user_id", None) or "")
        if hasattr(client, "_get_session_user_id"):
            owner_user_id = str(client._get_session_user_id() or owner_user_id)
        if not session_id:
            return visible or response_text

        if not await should_attempt_roleplay_generation(
            character_data=char_data,
            scene_description=scene_description,
            session_id=session_id,
            config=getattr(client, "config", None),
        ):
            return visible or response_text

        appearance_tags = ""
        negative_tags = ""
        comfyui_overrides: dict[str, Any] = {}
        character_type = ""
        if char_data:
            appearance_tags = char_data.get("appearance_tags", "")
            negative_tags = char_data.get("negative_tags", "")
            comfyui_overrides = char_data.get("comfyui_config", {}) or {}
            character_type = str(char_data.get("character_type") or "")

        history_messages = history
        if history_messages is None:
            history_manager = getattr(client, "history_manager", None)
            if history_manager is not None and hasattr(history_manager, "get_all"):
                history_messages = history_manager.get_all()
            else:
                history_messages = list(getattr(client, "conversation_history", []) or [])

        prompt, default_negative = await build_image_prompt(
            history_messages,
            appearance_tags,
            scene_description,
            usage_context=client,
            roleplay_pov=character_type == "roleplay",
        )
        negative_parts = [part for part in [negative_tags, default_negative] if part]
        combined_negative = ", ".join(negative_parts)

        logger.info("[RoleplaySceneImage] シーン画像生成開始: %s...", prompt[:80])
        image_event = await generate_roleplay_scene_media(
            owner_user_id=owner_user_id,
            session_id=session_id,
            message_id=assistant_message_id,
            scene_description=scene_description,
            positive_prompt=prompt,
            negative_prompt=combined_negative,
            comfyui_overrides=comfyui_overrides,
            engine="comfyui",
            config=getattr(client, "config", None),
        )
        if image_event and image_event.get("tag"):
            if stream_callback:
                payload = {
                    **image_event,
                    "session_id": session_id,
                    "message_id": assistant_message_id,
                }
                try:
                    result = stream_callback("generated_image", payload)
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    logger.warning("Roleplay 画像 stream 通知に失敗", exc_info=True)
            return f"{visible}\n\n{image_event['tag']}".strip()
    except Exception:
        logger.error("Roleplay シーン画像生成に失敗", exc_info=True)

    return visible or response_text
