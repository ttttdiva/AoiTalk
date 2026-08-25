"""TRPG Play 卓画像の設定正規化と生成オーケストレーション。"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Mapping, Sequence

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models.generated_media import GeneratedMedia
from ..memory.models.story import StoryCharacter, StoryWork, StoryWorkCharacter
from ..memory.models.trpg_play import TrpgPlayEvent, TrpgPlayParticipant, TrpgPlaySession
from .generated_media_service import (
    generate_play_context_media,
    is_comfyui_enabled,
    resolve_engine,
)
from .story_illustration_service import build_visual_prompt
from .trpg_play_image_triggers import (
    detect_play_image_trigger,
    prompts_are_similar,
    snapshot_scene_key,
)

logger = logging.getLogger(__name__)

DEFAULT_PLAY_IMAGE_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "engine": "comfyui",
    "workflow_path": None,
    "style": "",
    "negative_prompt": "",
}

PLAY_IMAGE_COOLDOWN_SECONDS = 45

BroadcastEventFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class PlayImageBackgroundJob:
    session_id: uuid.UUID
    event_id: uuid.UUID
    trigger: str
    action_text: str
    narration_text: str
    previous_snapshot: Mapping[str, Any] | None
    manual_prompt: str | None
    owner_user_id: str


def normalize_play_image_settings(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    settings = dict(DEFAULT_PLAY_IMAGE_SETTINGS)
    if isinstance(raw, Mapping):
        settings.update(raw)
    engine = str(settings.get("engine") or "").strip().lower()
    settings["engine"] = engine if resolve_engine(engine) else ""
    settings["enabled"] = bool(settings.get("enabled"))
    settings["style"] = str(settings.get("style") or "")
    settings["negative_prompt"] = str(settings.get("negative_prompt") or "")
    workflow = settings.get("workflow_path")
    settings["workflow_path"] = str(workflow).strip() if workflow else None
    return settings


def is_play_image_enabled(raw: Mapping[str, Any] | None) -> bool:
    return bool(normalize_play_image_settings(raw).get("enabled"))


async def _load_work_characters(session: AsyncSession, work_id: Any) -> list[StoryCharacter]:
    work_chars = (
        await session.execute(
            select(StoryWorkCharacter).where(StoryWorkCharacter.work_id == work_id)
        )
    ).scalars().all()
    char_ids = [item.character_id for item in work_chars if item.character_id]
    if not char_ids:
        return []
    return list(
        (
            await session.execute(
                select(StoryCharacter).where(StoryCharacter.id.in_(char_ids))
            )
        ).scalars().all()
    )


async def _last_success_media(
    session: AsyncSession,
    session_id: Any,
) -> GeneratedMedia | None:
    return (
        await session.execute(
            select(GeneratedMedia)
            .where(
                GeneratedMedia.context_type == "trpg_play",
                GeneratedMedia.context_id == str(session_id),
                GeneratedMedia.status == "succeeded",
            )
            .order_by(desc(GeneratedMedia.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()


def _cooldown_active(last_media: GeneratedMedia | None) -> bool:
    if last_media is None or last_media.created_at is None:
        return False
    elapsed = datetime.utcnow() - last_media.created_at.replace(tzinfo=None)
    return elapsed < timedelta(seconds=PLAY_IMAGE_COOLDOWN_SECONDS)


def _build_scene_description(
    *,
    trigger: str,
    action_text: str,
    narration_text: str,
    snapshot: Mapping[str, Any] | None,
) -> str:
    scene_key = snapshot_scene_key(snapshot)
    parts: list[str] = []
    if scene_key:
        parts.append(f"scene: {scene_key}")
    if narration_text.strip():
        parts.append(narration_text.strip())
    elif action_text.strip():
        parts.append(action_text.strip())
    if trigger == "manual" and not parts:
        parts.append("TRPG tabletop scene")
    return " / ".join(parts).strip()


def _event_dict_for_broadcast(
    event: TrpgPlayEvent,
    participants: Sequence[TrpgPlayParticipant],
) -> dict[str, Any]:
    name_map = {str(item.id): item.display_name for item in participants}
    return event.to_dict(
        actor_display_name=name_map.get(str(event.actor_participant_id or ""))
    )


class TrpgPlayImageService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        config: Any | None = None,
        comfy_generate: Any | None = None,
    ):
        self.session = session
        self.config = config
        self.comfy_generate = comfy_generate

    async def should_generate(
        self,
        session_row: TrpgPlaySession,
        *,
        trigger_hint: str | None = None,
        action_text: str = "",
        narration_text: str = "",
        previous_snapshot: Mapping[str, Any] | None = None,
    ) -> str | None:
        settings = normalize_play_image_settings(session_row.image_settings)
        if not settings.get("enabled"):
            return None
        if resolve_engine(settings.get("engine")) != "comfyui":
            return None
        if not is_comfyui_enabled(self.config):
            return None

        trigger = detect_play_image_trigger(
            trigger_hint=trigger_hint,
            action_text=action_text,
            narration_text=narration_text,
            previous_snapshot=previous_snapshot,
            current_snapshot=session_row.snapshot or {},
        )
        if trigger is None:
            return None

        last_media = await _last_success_media(self.session, session_row.id)
        if trigger != "manual" and _cooldown_active(last_media):
            return None

        scene = _build_scene_description(
            trigger=trigger,
            action_text=action_text,
            narration_text=narration_text,
            snapshot=session_row.snapshot or {},
        )
        prev_prompt = ""
        if last_media and isinstance(last_media.prompt_meta, dict):
            prev_prompt = str(last_media.prompt_meta.get("prompt") or "")
        if trigger != "manual" and prompts_are_similar(prev_prompt, scene):
            return None
        return trigger

    async def prepare_for_event(
        self,
        session_row: TrpgPlaySession,
        event: TrpgPlayEvent,
        *,
        trigger_hint: str | None = None,
        action_text: str = "",
        narration_text: str = "",
        previous_snapshot: Mapping[str, Any] | None = None,
        manual_prompt: str | None = None,
        owner_user_id: str | None = None,
    ) -> PlayImageBackgroundJob | None:
        trigger = await self.should_generate(
            session_row,
            trigger_hint=trigger_hint,
            action_text=action_text,
            narration_text=narration_text,
            previous_snapshot=previous_snapshot,
        )
        if trigger is None and trigger_hint == "manual":
            settings = normalize_play_image_settings(session_row.image_settings)
            if not settings.get("enabled"):
                return None
            trigger = "manual"
        if trigger is None:
            return None

        work = await self.session.get(StoryWork, session_row.work_id)
        if work is None:
            return None

        scene_description = (
            manual_prompt.strip()
            if manual_prompt and trigger == "manual"
            else _build_scene_description(
                trigger=trigger,
                action_text=action_text,
                narration_text=narration_text,
                snapshot=session_row.snapshot or {},
            )
        )
        if not scene_description:
            return None

        meta = dict(event.meta or {})
        meta["image_trigger"] = trigger
        meta["image_status"] = "pending"
        event.meta = meta
        await self.session.flush()

        owner = owner_user_id or str(session_row.host_user_id)
        return PlayImageBackgroundJob(
            session_id=session_row.id,
            event_id=event.id,
            trigger=trigger,
            action_text=action_text,
            narration_text=narration_text,
            previous_snapshot=dict(previous_snapshot) if previous_snapshot else None,
            manual_prompt=manual_prompt,
            owner_user_id=owner,
        )

    async def execute_background_job(self, job: PlayImageBackgroundJob) -> str | None:
        session_row = await self.session.get(TrpgPlaySession, job.session_id)
        event = await self.session.get(TrpgPlayEvent, job.event_id)
        if session_row is None or event is None:
            return None

        work = await self.session.get(StoryWork, session_row.work_id)
        if work is None:
            await self._mark_image_failed(event)
            return None

        settings = normalize_play_image_settings(session_row.image_settings)
        characters = await _load_work_characters(self.session, session_row.work_id)
        scene_description = (
            job.manual_prompt.strip()
            if job.manual_prompt and job.trigger == "manual"
            else _build_scene_description(
                trigger=job.trigger,
                action_text=job.action_text,
                narration_text=job.narration_text,
                snapshot=session_row.snapshot or {},
            )
        )
        if not scene_description:
            await self._mark_image_failed(event)
            return None

        positive, negative = build_visual_prompt(
            scene_description=scene_description,
            work=work,
            characters=characters,
            image_settings=settings,
        )

        comfy_overrides: dict[str, Any] = {}
        if settings.get("workflow_path"):
            comfy_overrides["workflow_path"] = settings["workflow_path"]

        media_id = await generate_play_context_media(
            owner_user_id=job.owner_user_id,
            session_id=str(session_row.id),
            bind_type="play_event",
            bind_id=str(event.id),
            positive_prompt=positive,
            negative_prompt=negative,
            scene_description=scene_description,
            comfyui_overrides=comfy_overrides,
            engine="comfyui",
            config=self.config,
            comfy_generate=self.comfy_generate,
        )

        meta = dict(event.meta or {})
        if media_id:
            meta["generated_media_id"] = media_id
            meta["image_status"] = "succeeded"
            if job.trigger == "manual":
                event.body = "場面画像を生成しました。"
        else:
            meta["image_status"] = "failed"
            if job.trigger == "manual":
                event.body = "場面画像の生成に失敗したか、設定が OFF です。"
        event.meta = meta
        await self.session.flush()
        return media_id

    async def _mark_image_failed(self, event: TrpgPlayEvent) -> None:
        meta = dict(event.meta or {})
        meta["image_status"] = "failed"
        event.meta = meta
        if str(meta.get("image_trigger") or "") == "manual":
            event.body = "場面画像の生成に失敗したか、設定が OFF です。"
        await self.session.flush()


async def run_play_image_background(
    job: PlayImageBackgroundJob,
    *,
    config: Any | None = None,
    broadcast_event: BroadcastEventFn | None = None,
) -> None:
    """commit / WS 配信後に卓画像を生成する。失敗しても卓は継続する。"""
    from ..memory.database import get_db_session

    async with await get_db_session() as session:
        try:
            image_service = TrpgPlayImageService(session, config=config)
            await image_service.execute_background_job(job)
            await session.commit()

            if broadcast_event is not None:
                event = await session.get(TrpgPlayEvent, job.event_id)
                if event is not None:
                    participants = (
                        await session.execute(
                            select(TrpgPlayParticipant).where(
                                TrpgPlayParticipant.session_id == job.session_id
                            )
                        )
                    ).scalars().all()
                    await broadcast_event(
                        str(job.session_id),
                        _event_dict_for_broadcast(event, participants),
                    )
        except Exception:
            logger.exception(
                "TRPG Play 画像バックグラウンド生成でエラー（卓は継続）: %s",
                job.event_id,
            )
            await session.rollback()
            try:
                event = await session.get(TrpgPlayEvent, job.event_id)
                if event is not None:
                    image_service = TrpgPlayImageService(session, config=config)
                    await image_service._mark_image_failed(event)
                    await session.commit()
                    if broadcast_event is not None:
                        participants = (
                            await session.execute(
                                select(TrpgPlayParticipant).where(
                                    TrpgPlayParticipant.session_id == job.session_id
                                )
                            )
                        ).scalars().all()
                        await broadcast_event(
                            str(job.session_id),
                            _event_dict_for_broadcast(event, participants),
                        )
            except Exception:
                logger.exception(
                    "TRPG Play 画像失敗マーク更新にも失敗: %s",
                    job.event_id,
                )
                await session.rollback()


__all__ = [
    "DEFAULT_PLAY_IMAGE_SETTINGS",
    "PlayImageBackgroundJob",
    "TrpgPlayImageService",
    "is_play_image_enabled",
    "normalize_play_image_settings",
    "run_play_image_background",
]
