"""生成メディアの永続化・配信・Roleplay 画像生成。"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from sqlalchemy import desc, func, select

from ..memory.database import get_db_session
from ..memory.models.conversations import ConversationMessage
from ..memory.models.generated_media import GeneratedMedia
from .generated_media_triggers import coerce_trigger, should_generate_roleplay_image

logger = logging.getLogger(__name__)

STORAGE_ROOT = Path("data/generated_media")
SUPPORTED_ENGINES = frozenset({"comfyui"})
ComfyGenerateFunc = Callable[..., Any]


def resolve_engine(engine: str | None) -> str | None:
    """実装済み画像 engine のみ解決する。"""
    normalized = str(engine or "").strip().lower()
    if normalized in SUPPORTED_ENGINES:
        return normalized
    return None


def is_comfyui_enabled(config: Any | None) -> bool:
    if config is None:
        return True
    comfyui_conf = config.get("comfyui", {}) if hasattr(config, "get") else {}
    if not isinstance(comfyui_conf, dict):
        return True
    return bool(comfyui_conf.get("enabled", True))


def _storage_root() -> Path:
    root = STORAGE_ROOT.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _relative_path(storage_key: str, extension: str) -> str:
    return f"generated_media/{storage_key}{extension}"


def _absolute_path(relative_path: str) -> Path:
    return (_storage_root().parent / relative_path).resolve()


def media_public_url(media_id: str) -> str:
    return f"/api/generated-media/{media_id}"


def generated_image_tag(media_id: str) -> str:
    return f"[GENERATED_IMAGE:{media_id}]"


async def get_media_record(media_id: str) -> GeneratedMedia | None:
    uid = _parse_uuid(media_id)
    if uid is None:
        return None
    async with await get_db_session() as session:
        return await session.get(GeneratedMedia, uid)


async def user_can_access_media(user_id: str, media: GeneratedMedia) -> bool:
    if not user_id:
        return False
    if media.context_type == "trpg_play":
        from ..memory.models.trpg_play import TrpgPlayParticipant

        async with await get_db_session() as session:
            participant = (
                await session.execute(
                    select(TrpgPlayParticipant).where(
                        TrpgPlayParticipant.session_id == _parse_uuid(media.context_id),
                        TrpgPlayParticipant.user_id == _parse_uuid(user_id),
                        TrpgPlayParticipant.left_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            return participant is not None
    if str(media.owner_user_id) == str(user_id):
        return True
    if media.context_type == "conversation":
        from ..memory.models.conversations import ConversationSession

        async with await get_db_session() as session:
            session_row = await session.get(
                ConversationSession, _parse_uuid(media.context_id)
            )
            if session_row is None:
                return False
            return str(session_row.user_id) == str(user_id)
    if media.context_type == "story":
        from ..memory.models.story import StoryWork

        async with await get_db_session() as session:
            work = await session.get(StoryWork, _parse_uuid(media.context_id))
            if work is None:
                return False
            return str(work.user_id) == str(user_id)
    return False


async def get_last_successful_scene_state(
    context_type: str,
    context_id: str,
) -> tuple[str | None, int]:
    """直近成功メディアの scene と、その後の user-assistant 往復数を返す。"""
    async with await get_db_session() as session:
        last_media = (
            await session.execute(
                select(GeneratedMedia)
                .where(
                    GeneratedMedia.context_type == context_type,
                    GeneratedMedia.context_id == context_id,
                    GeneratedMedia.status == "succeeded",
                )
                .order_by(desc(GeneratedMedia.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()

        if last_media is None:
            return None, 0

        previous_scene = ""
        prompt_meta = last_media.prompt_meta or {}
        if isinstance(prompt_meta, dict):
            previous_scene = str(prompt_meta.get("scene") or "")

        since = last_media.created_at
        if since is None:
            return previous_scene or None, 0

        user_turns = (
            await session.execute(
                select(func.count())
                .select_from(ConversationMessage)
                .where(
                    ConversationMessage.session_id == _parse_uuid(context_id),
                    ConversationMessage.role == "user",
                    ConversationMessage.created_at > since,
                )
            )
        ).scalar_one()
        return previous_scene or None, int(user_turns or 0)


async def purge_stale_generated_media(
    now: datetime | None = None,
    failed_after_days: int = 7,
) -> dict[str, int]:
    """failed/pending の古い生成メディアを削除する。

    succeeded は bind/context がある限り保持する方針のため、この関数では削除しない。
    """
    current = now or datetime.utcnow()
    cutoff = current - timedelta(days=max(1, int(failed_after_days)))
    deleted_rows = 0
    deleted_files = 0
    storage_root = _storage_root()

    async with await get_db_session() as session:
        stale = (
            await session.execute(
                select(GeneratedMedia).where(
                    GeneratedMedia.status.in_(("failed", "pending")),
                    GeneratedMedia.created_at < cutoff,
                )
            )
        ).scalars().all()

        for media in stale:
            file_path = _absolute_path(media.relative_path)
            if file_path.exists() and storage_root in file_path.parents:
                try:
                    file_path.unlink()
                    deleted_files += 1
                except OSError as exc:
                    logger.warning("生成メディアファイル削除失敗: %s", exc)
            await session.delete(media)
            deleted_rows += 1

        if deleted_rows:
            await session.commit()

    return {"deleted_rows": deleted_rows, "deleted_files": deleted_files}


async def should_attempt_roleplay_generation(
    *,
    character_data: dict[str, Any] | None,
    scene_description: str,
    session_id: str,
    config: Any | None = None,
) -> bool:
    char = character_data or {}
    if not bool(char.get("auto_image_gen")):
        return False
    if resolve_engine(char.get("image_gen_engine")) != "comfyui":
        return False
    if not is_comfyui_enabled(config):
        return False

    previous_scene, turns_since_last = await get_last_successful_scene_state(
        "conversation",
        session_id,
    )
    return should_generate_roleplay_image(
        trigger=coerce_trigger(char.get("image_gen_trigger")),
        interval=int(char.get("image_gen_interval") or 5),
        scene_description=scene_description,
        previous_scene=previous_scene,
        turns_since_last_success=turns_since_last,
    )


async def generate_roleplay_scene_media(
    *,
    owner_user_id: str,
    session_id: str,
    message_id: str,
    scene_description: str,
    positive_prompt: str,
    negative_prompt: str,
    comfyui_overrides: dict[str, Any] | None = None,
    engine: str = "comfyui",
    config: Any | None = None,
    comfy_generate: ComfyGenerateFunc | None = None,
) -> dict[str, Any] | None:
    """Roleplay シーン画像を生成し、永続 media レコードを返す。"""
    if not owner_user_id or not session_id or not message_id:
        return None

    media_id = uuid.uuid4()
    storage_key = uuid.uuid4().hex
    extension = ".png"
    relative = _relative_path(storage_key, extension)
    prompt_meta = {
        "scene": scene_description,
        "prompt": positive_prompt,
        "negative": negative_prompt,
        "engine": engine,
        "workflow": (comfyui_overrides or {}).get("workflow_path"),
    }

    async with await get_db_session() as session:
        record = GeneratedMedia(
            id=media_id,
            owner_user_id=str(owner_user_id),
            context_type="conversation",
            context_id=str(session_id),
            bind_type="message",
            bind_id=str(message_id),
            storage_key=storage_key,
            mime_type="image/png",
            relative_path=relative,
            status="pending",
            prompt_meta=prompt_meta,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)

    try:
        image_bytes, mime_type, saved_extension = await _generate_with_comfyui(
            positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
            overrides=comfyui_overrides or {},
            config=config,
            comfy_generate=comfy_generate,
        )
        if saved_extension and saved_extension != extension:
            extension = saved_extension
            relative = _relative_path(storage_key, extension)
        destination = _absolute_path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(image_bytes)

        async with await get_db_session() as session:
            persisted = await session.get(GeneratedMedia, media_id)
            if persisted is None:
                return None
            persisted.status = "succeeded"
            persisted.mime_type = mime_type
            persisted.byte_size = len(image_bytes)
            persisted.relative_path = relative
            persisted.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(persisted)
            return _event_payload(persisted, session_id=session_id, message_id=message_id)
    except Exception as exc:
        logger.warning("Roleplay 画像生成に失敗しました: %s", exc)
        async with await get_db_session() as session:
            persisted = await session.get(GeneratedMedia, media_id)
            if persisted is not None:
                persisted.status = "failed"
                persisted.error_message = str(exc)
                persisted.updated_at = datetime.utcnow()
                await session.commit()
        return None


async def generate_play_context_media(
    *,
    owner_user_id: str,
    session_id: str,
    bind_type: str,
    bind_id: str,
    positive_prompt: str,
    negative_prompt: str,
    scene_description: str = "",
    comfyui_overrides: dict[str, Any] | None = None,
    engine: str = "comfyui",
    config: Any | None = None,
    comfy_generate: ComfyGenerateFunc | None = None,
) -> str | None:
    """TRPG Play 卓画像を生成し、media ID を返す。"""
    if not owner_user_id or not session_id or not bind_id:
        return None

    media_id = uuid.uuid4()
    storage_key = uuid.uuid4().hex
    extension = ".png"
    relative = _relative_path(storage_key, extension)
    prompt_meta = {
        "scene": scene_description,
        "prompt": positive_prompt,
        "negative": negative_prompt,
        "engine": engine,
        "workflow": (comfyui_overrides or {}).get("workflow_path"),
    }

    async with await get_db_session() as session:
        record = GeneratedMedia(
            id=media_id,
            owner_user_id=str(owner_user_id),
            context_type="trpg_play",
            context_id=str(session_id),
            bind_type=bind_type,
            bind_id=str(bind_id),
            storage_key=storage_key,
            mime_type="image/png",
            relative_path=relative,
            status="pending",
            prompt_meta=prompt_meta,
        )
        session.add(record)
        await session.commit()

    try:
        image_bytes, mime_type, saved_extension = await _generate_with_comfyui(
            positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
            overrides=comfyui_overrides or {},
            config=config,
            comfy_generate=comfy_generate,
        )
        if saved_extension and saved_extension != extension:
            extension = saved_extension
            relative = _relative_path(storage_key, extension)
        destination = _absolute_path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(image_bytes)

        async with await get_db_session() as session:
            persisted = await session.get(GeneratedMedia, media_id)
            if persisted is None:
                return None
            persisted.status = "succeeded"
            persisted.mime_type = mime_type
            persisted.byte_size = len(image_bytes)
            persisted.relative_path = relative
            persisted.updated_at = datetime.utcnow()
            await session.commit()
            return str(media_id)
    except Exception as exc:
        logger.warning("TRPG Play 画像生成に失敗しました: %s", exc)
        async with await get_db_session() as session:
            persisted = await session.get(GeneratedMedia, media_id)
            if persisted is not None:
                persisted.status = "failed"
                persisted.error_message = str(exc)
                persisted.updated_at = datetime.utcnow()
                await session.commit()
        return None


async def generate_story_context_media(
    *,
    owner_user_id: str,
    work_id: str,
    bind_type: str,
    bind_id: str,
    positive_prompt: str,
    negative_prompt: str,
    scene_description: str = "",
    comfyui_overrides: dict[str, Any] | None = None,
    engine: str = "comfyui",
    config: Any | None = None,
    comfy_generate: ComfyGenerateFunc | None = None,
) -> str | None:
    """Story 文脈の画像を生成し、media ID を返す。"""
    if not owner_user_id or not work_id or not bind_id:
        return None

    media_id = uuid.uuid4()
    storage_key = uuid.uuid4().hex
    extension = ".png"
    relative = _relative_path(storage_key, extension)
    prompt_meta = {
        "scene": scene_description,
        "prompt": positive_prompt,
        "negative": negative_prompt,
        "engine": engine,
        "workflow": (comfyui_overrides or {}).get("workflow_path"),
    }

    async with await get_db_session() as session:
        record = GeneratedMedia(
            id=media_id,
            owner_user_id=str(owner_user_id),
            context_type="story",
            context_id=str(work_id),
            bind_type=bind_type,
            bind_id=str(bind_id),
            storage_key=storage_key,
            mime_type="image/png",
            relative_path=relative,
            status="pending",
            prompt_meta=prompt_meta,
        )
        session.add(record)
        await session.commit()

    try:
        image_bytes, mime_type, saved_extension = await _generate_with_comfyui(
            positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
            overrides=comfyui_overrides or {},
            config=config,
            comfy_generate=comfy_generate,
        )
        if saved_extension and saved_extension != extension:
            extension = saved_extension
            relative = _relative_path(storage_key, extension)
        destination = _absolute_path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(image_bytes)

        async with await get_db_session() as session:
            persisted = await session.get(GeneratedMedia, media_id)
            if persisted is None:
                return None
            persisted.status = "succeeded"
            persisted.mime_type = mime_type
            persisted.byte_size = len(image_bytes)
            persisted.relative_path = relative
            persisted.updated_at = datetime.utcnow()
            await session.commit()
            return str(media_id)
    except Exception as exc:
        logger.warning("Story 画像生成に失敗しました: %s", exc)
        async with await get_db_session() as session:
            persisted = await session.get(GeneratedMedia, media_id)
            if persisted is not None:
                persisted.status = "failed"
                persisted.error_message = str(exc)
                persisted.updated_at = datetime.utcnow()
                await session.commit()
        return None


async def _generate_with_comfyui(
    *,
    positive_prompt: str,
    negative_prompt: str,
    overrides: dict[str, Any],
    config: Any | None,
    comfy_generate: ComfyGenerateFunc | None,
) -> tuple[bytes, str, str]:
    if comfy_generate is None:
        from .comfyui_service import generate_image as comfy_generate

    result = await comfy_generate(
        prompt=positive_prompt,
        negative_prompt=negative_prompt,
        workflow_path=overrides.get("workflow_path"),
        overrides=overrides,
    )
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError("ComfyUI 画像生成に失敗しました")

    source_path = Path(str(result.get("image_path") or ""))
    if not source_path.exists():
        raise RuntimeError("ComfyUI 出力ファイルが見つかりません")

    mime_type = mimetypes.guess_type(source_path.name)[0] or "image/png"
    extension = source_path.suffix or ".png"
    data = source_path.read_bytes()
    return data, mime_type, extension


def resolve_media_file(media: GeneratedMedia) -> Path | None:
    if media.status != "succeeded":
        return None
    path = _absolute_path(media.relative_path)
    if not path.exists():
        return None
    root = _storage_root().resolve()
    if root not in path.parents and path != root:
        return None
    return path


def _event_payload(
    media: GeneratedMedia,
    *,
    session_id: str,
    message_id: str,
) -> dict[str, Any]:
    media_id = str(media.id)
    tag = generated_image_tag(media_id)
    return {
        "content": tag,
        "tag": tag,
        "media_id": media_id,
        "session_id": session_id,
        "message_id": message_id,
        "image_url": media_public_url(media_id),
        "status": media.status,
    }


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
