"""Scenario Studio 挿絵サービス。

小説・TRPG 共通。本文は書き換えず、story_illustrations 行と generated_media が正本。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models.generated_media import GeneratedMedia
from ..memory.models.story import (
    StoryCharacter,
    StoryEpisode,
    StoryIllustration,
    StoryWork,
    StoryWorkCharacter,
)
from .generated_media_service import (
    _absolute_path,
    generate_story_context_media,
    is_comfyui_enabled,
    media_public_url,
    resolve_engine,
)

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "engine": "comfyui",
    "max_images_per_episode": 3,
    "workflow_path": None,
    "style": "",
    "negative_prompt": "",
}


def normalize_image_settings(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    settings = dict(DEFAULT_IMAGE_SETTINGS)
    if isinstance(raw, Mapping):
        settings.update(raw)
    max_images = settings.get("max_images_per_episode")
    try:
        settings["max_images_per_episode"] = max(0, min(10, int(max_images)))
    except (TypeError, ValueError):
        settings["max_images_per_episode"] = DEFAULT_IMAGE_SETTINGS["max_images_per_episode"]
    engine = str(settings.get("engine") or "").strip().lower()
    settings["engine"] = engine if resolve_engine(engine) else ""
    settings["enabled"] = bool(settings.get("enabled"))
    settings["style"] = str(settings.get("style") or "")
    settings["negative_prompt"] = str(settings.get("negative_prompt") or "")
    workflow = settings.get("workflow_path")
    settings["workflow_path"] = str(workflow).strip() if workflow else None
    return settings


def is_image_settings_enabled(raw: Mapping[str, Any] | None) -> bool:
    return bool(normalize_image_settings(raw).get("enabled"))


def resolve_illustration_anchor(
    body: str,
    quote: str,
    offset_hint: int | None = None,
) -> dict[str, Any]:
    """本文中の anchor_quote を解決する。

    0 件一致、または複数一致で offset_hint がない場合は stale。
    複数一致かつ offset_hint がある場合は、hint に最も近い出現位置を採用する
    （同距離なら小さい index）。最近傍でも hint から十分離れている場合は、
    別箇所の同名引用を誤採用しないよう stale とする。
    """
    text = body or ""
    needle = (quote or "").strip()
    if not needle or not text:
        return {"index": None, "stale": True}

    positions: list[int] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            break
        positions.append(index)
        start = index + 1

    if not positions:
        return {"index": None, "stale": True}
    if len(positions) == 1:
        return {"index": positions[0], "stale": False}
    if offset_hint is None:
        return {"index": None, "stale": True}

    nearest = min(positions, key=lambda pos: (abs(pos - offset_hint), pos))
    # 本文編集で引用が大きく移動した場合、無関係な一致を拾わないための距離上限。
    max_distance = max(len(needle) * 8, 400)
    if abs(nearest - offset_hint) > max_distance:
        return {"index": None, "stale": True}
    return {"index": nearest, "stale": False}


def _strip_code_fences(text: str) -> str:
    """散文や未閉じフェンスを含む応答から JSON 本体を取り出す。"""
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)(?:\s*```|$)", stripped, re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _extract_balanced_json_span(text: str, start: int) -> str | None:
    """文字列リテラル内の括弧を無視し、対応する括弧までの JSON 断片を返す。"""
    if start < 0 or start >= len(text) or text[start] not in "[{":
        return None

    pairs = {"[": "]", "{": "}"}
    stack: list[str] = []
    in_string = False
    escape = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char in pairs:
            stack.append(char)
            continue
        if stack and char == pairs[stack[-1]]:
            stack.pop()
            if not stack:
                return text[start : index + 1]
    return None


def _first_json_span(text: str) -> str | None:
    for index, char in enumerate(text):
        if char in "[{":
            return _extract_balanced_json_span(text, index)
    return None


def _salvage_complete_json_objects(text: str) -> list[dict[str, Any]]:
    """途中で切れた配列から、閉じているオブジェクトだけを拾う。"""
    salvaged: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        brace = text.find("{", index)
        if brace < 0:
            break
        span = _extract_balanced_json_span(text, brace)
        if span is None:
            break
        try:
            payload = json.loads(span)
        except json.JSONDecodeError:
            index = brace + 1
            continue
        if isinstance(payload, dict):
            salvaged.append(payload)
        index = brace + len(span)
    return salvaged


def _candidate_items_from_payload(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        items = payload.get("illustrations") or payload.get("candidates") or []
        return list(items) if isinstance(items, list) else []
    if isinstance(payload, list):
        return payload
    return []


def _normalize_candidate_items(items: Sequence[Any], *, max_count: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        scene = str(item.get("scene_description") or item.get("scene") or "").strip()
        quote = str(item.get("anchor_quote") or item.get("quote") or "").strip()
        if not scene or not quote:
            continue
        if len(quote) < 8:
            continue
        if len(quote) > 200:
            quote = quote[:200]
        results.append({"scene_description": scene, "anchor_quote": quote})
        if len(results) >= max_count:
            break
    return results


def _decode_candidate_payload(raw: str) -> tuple[Any | None, bool]:
    """候補 JSON を段階的にデコードする。戻り値は (payload, parsed_ok)。"""
    cleaned = _strip_code_fences(raw)
    candidates = [cleaned]
    span = _first_json_span(cleaned)
    if span and span not in candidates:
        candidates.append(span)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate), True
        except json.JSONDecodeError:
            continue

    salvage_source = span or cleaned
    salvaged = _salvage_complete_json_objects(salvage_source)
    if salvaged:
        return salvaged, True
    return None, False


def _parse_candidate_payload(raw: str, *, max_count: int) -> list[dict[str, str]]:
    payload, parsed_ok = _decode_candidate_payload(raw)
    if not parsed_ok or payload is None:
        logger.warning(
            "挿絵候補 JSON のパースに失敗しました。raw_head=%r",
            (raw or "")[:300],
        )
        return []

    items = _candidate_items_from_payload(payload)
    if not items and isinstance(payload, list):
        items = payload
    return _normalize_candidate_items(items, max_count=max_count)


def build_visual_prompt(
    *,
    scene_description: str,
    work: StoryWork,
    characters: Sequence[StoryCharacter],
    image_settings: Mapping[str, Any],
) -> tuple[str, str]:
    settings = normalize_image_settings(image_settings)
    style = str(settings.get("style") or work.style_guide or "").strip()
    negative = str(settings.get("negative_prompt") or "").strip()
    appearance_lines = []
    for character in characters:
        parts = [character.name]
        if character.description:
            parts.append(character.description.strip())
        if character.image_path:
            parts.append(f"(reference image: {character.image_path})")
        appearance_lines.append(" / ".join(parts))
    positive_parts = [scene_description.strip()]
    if style:
        positive_parts.append(f"style: {style}")
    if appearance_lines:
        positive_parts.append("characters: " + "; ".join(appearance_lines))
    return ", ".join(part for part in positive_parts if part), negative


async def extract_illustration_candidates(
    *,
    body: str,
    max_count: int,
    generate_text: Callable[[str], Awaitable[str]],
) -> list[dict[str, str]]:
    if max_count <= 0 or not (body or "").strip():
        return []
    prompt = (
        "あなたは小説・シナリオの挿絵プランナーです。"
        f"次の本文から挿絵に値する場面を最大{max_count}件選び、JSON 配列だけを返してください。\n"
        '各要素は {"scene_description":"英語または日本語の情景説明","anchor_quote":"本文からの引用40-160字"} です。\n'
        "anchor_quote は本文にそのまま存在し、できるだけ一意になるよう前後の文を含めてください。\n"
        "場面が乏しい場合は空配列 [] を返してください。\n\n"
        f"本文:\n{body}"
    )
    try:
        raw = await generate_text(prompt)
    except Exception as exc:
        logger.warning("挿絵候補の LLM 抽出に失敗: %s", exc)
        return []
    results = _parse_candidate_payload(raw, max_count=max_count)
    if not results and (raw or "").strip() and (raw or "").strip() != "[]":
        logger.warning(
            "挿絵候補が 0 件でした。raw_head=%r",
            (raw or "")[:300],
        )
    return results


class StoryIllustrationService:
    def __init__(self, session: AsyncSession, *, config: Any | None = None):
        self.session = session
        self.config = config

    async def list_for_episode(self, episode: StoryEpisode, *, body: str | None = None) -> dict[str, Any]:
        rows = list(
            (
                await self.session.scalars(
                    select(StoryIllustration)
                    .where(StoryIllustration.episode_id == episode.id)
                    .order_by(StoryIllustration.ordering, StoryIllustration.created_at)
                )
            ).all()
        )
        current_body = body if body is not None else (episode.body or "")
        active: list[dict[str, Any]] = []
        stale_items: list[dict[str, Any]] = []
        for row in rows:
            resolved = resolve_illustration_anchor(current_body, row.anchor_quote, row.offset_hint)
            item = row.to_dict(
                resolved_index=resolved["index"],
                stale=resolved["stale"] or row.status == "stale",
            )
            if item.get("generated_media_id"):
                item["image_url"] = media_public_url(str(item["generated_media_id"]))
            if resolved["stale"] or row.status == "stale":
                stale_items.append(item)
            else:
                active.append(item)
        return {"active": active, "stale": stale_items}

    async def resolve_episode_anchors(self, episode: StoryEpisode) -> int:
        rows = list(
            (
                await self.session.scalars(
                    select(StoryIllustration).where(StoryIllustration.episode_id == episode.id)
                )
            ).all()
        )
        body = episode.body or ""
        changed = 0
        for row in rows:
            resolved = resolve_illustration_anchor(body, row.anchor_quote, row.offset_hint)
            next_status = "stale" if resolved["stale"] else row.status
            if resolved["stale"] and row.status != "stale":
                row.status = "stale"
                row.updated_at = datetime.utcnow()
                changed += 1
            elif not resolved["stale"] and row.status == "stale":
                row.status = "pending" if not row.generated_media_id else "succeeded"
                row.updated_at = datetime.utcnow()
                changed += 1
        if changed:
            await self.session.flush()
        return changed

    async def generate_for_episode(
        self,
        work: StoryWork,
        episode: StoryEpisode,
        *,
        generate_text: Callable[[str], Awaitable[str]] | None = None,
        replace_existing: bool = False,
    ) -> list[dict[str, Any]]:
        settings = normalize_image_settings(work.image_settings)
        if not settings.get("enabled"):
            return []
        engine = resolve_engine(settings.get("engine"))
        if engine != "comfyui":
            return []
        if not is_comfyui_enabled(self.config):
            return []

        if replace_existing:
            await self._delete_episode_illustrations(episode.id)

        body = episode.body or ""
        if not body.strip():
            return []
        max_count = int(settings.get("max_images_per_episode") or 0)
        if max_count <= 0:
            return []

        if generate_text is None:
            return []

        candidates = await extract_illustration_candidates(
            body=body,
            max_count=max_count,
            generate_text=generate_text,
        )
        if not candidates:
            return []

        characters = await self._work_characters(work.id)
        created: list[dict[str, Any]] = []
        for ordering, candidate in enumerate(candidates):
            resolved = resolve_illustration_anchor(body, candidate["anchor_quote"])
            row = StoryIllustration(
                id=uuid.uuid4(),
                work_id=work.id,
                episode_id=episode.id,
                body_etag=episode.body_etag or "",
                rev_no=episode.current_rev_no,
                anchor_kind="quote",
                anchor_quote=candidate["anchor_quote"],
                offset_hint=resolved["index"],
                ordering=ordering,
                scene_description=candidate["scene_description"],
                status="pending",
            )
            positive, negative = build_visual_prompt(
                scene_description=candidate["scene_description"],
                work=work,
                characters=characters,
                image_settings=settings,
            )
            row.visual_prompt = positive
            self.session.add(row)
            await self.session.flush()
            await self._generate_media_for_row(work, row, positive, negative, settings)
            created.append(row.to_dict(resolved_index=resolved["index"], stale=resolved["stale"]))
        await self.session.flush()
        return created

    async def regenerate(self, work: StoryWork, illustration_id: uuid.UUID) -> dict[str, Any] | None:
        row = await self.session.get(StoryIllustration, illustration_id)
        if row is None or row.work_id != work.id:
            return None
        episode = await self.session.get(StoryEpisode, row.episode_id)
        if episode is None:
            return None
        settings = normalize_image_settings(work.image_settings)
        positive = row.visual_prompt or row.scene_description or ""
        negative = str(settings.get("negative_prompt") or "")
        if not positive.strip():
            positive, negative = build_visual_prompt(
                scene_description=row.scene_description or "",
                work=work,
                characters=await self._work_characters(work.id),
                image_settings=settings,
            )
            row.visual_prompt = positive
        row.status = "pending"
        row.error_message = None
        row.body_etag = episode.body_etag or ""
        row.rev_no = episode.current_rev_no
        resolved = resolve_illustration_anchor(episode.body or "", row.anchor_quote, row.offset_hint)
        row.offset_hint = resolved["index"]
        await self._purge_media_file(row.generated_media_id)
        row.generated_media_id = None
        await self.session.flush()
        await self._generate_media_for_row(work, row, positive, negative, settings)
        await self.session.flush()
        item = row.to_dict(resolved_index=resolved["index"], stale=resolved["stale"])
        if item.get("generated_media_id"):
            item["image_url"] = media_public_url(str(item["generated_media_id"]))
        return item

    async def delete(self, work: StoryWork, illustration_id: uuid.UUID) -> bool:
        row = await self.session.get(StoryIllustration, illustration_id)
        if row is None or row.work_id != work.id:
            return False
        await self._purge_media_file(row.generated_media_id)
        await self.session.delete(row)
        await self.session.flush()
        return True

    async def _generate_media_for_row(
        self,
        work: StoryWork,
        row: StoryIllustration,
        positive: str,
        negative: str,
        settings: Mapping[str, Any],
    ) -> None:
        engine = resolve_engine(settings.get("engine"))
        if engine != "comfyui" or not is_comfyui_enabled(self.config):
            row.status = "failed"
            row.error_message = "ComfyUI が無効です"
            row.updated_at = datetime.utcnow()
            return
        overrides: dict[str, Any] = {}
        workflow = settings.get("workflow_path")
        if workflow:
            overrides["workflow_path"] = workflow
        try:
            media_id = await generate_story_context_media(
                owner_user_id=str(work.user_id),
                work_id=str(work.id),
                bind_type="illustration",
                bind_id=str(row.id),
                positive_prompt=positive,
                negative_prompt=negative,
                scene_description=row.scene_description or "",
                comfyui_overrides=overrides,
                config=self.config,
            )
        except Exception as exc:
            logger.warning("挿絵画像生成に失敗: %s", exc)
            row.status = "failed"
            row.error_message = str(exc)
            row.updated_at = datetime.utcnow()
            return
        if not media_id:
            row.status = "failed"
            row.error_message = "画像生成に失敗しました"
            row.updated_at = datetime.utcnow()
            return
        row.generated_media_id = uuid.UUID(str(media_id))
        row.status = "succeeded"
        row.error_message = None
        row.updated_at = datetime.utcnow()

    async def _work_characters(self, work_id: uuid.UUID) -> list[StoryCharacter]:
        links = list(
            (
                await self.session.scalars(
                    select(StoryWorkCharacter)
                    .where(StoryWorkCharacter.work_id == work_id)
                    .order_by(StoryWorkCharacter.position)
                )
            ).all()
        )
        characters: list[StoryCharacter] = []
        for link in links:
            character = await self.session.get(StoryCharacter, link.character_id)
            if character is not None and character.archived_at is None:
                characters.append(character)
        return characters

    async def _delete_episode_illustrations(self, episode_id: uuid.UUID) -> None:
        rows = list(
            (
                await self.session.scalars(
                    select(StoryIllustration).where(StoryIllustration.episode_id == episode_id)
                )
            ).all()
        )
        for row in rows:
            await self._purge_media_file(row.generated_media_id)
            await self.session.delete(row)
        await self.session.flush()

    async def _purge_media_file(self, media_id: uuid.UUID | None) -> None:
        if media_id is None:
            return
        media = await self.session.get(GeneratedMedia, media_id)
        if media is None:
            return
        file_path = _absolute_path(media.relative_path)
        if file_path.exists():
            try:
                file_path.unlink()
            except OSError as exc:
                logger.warning("挿絵メディアファイル削除失敗: %s", exc)
        await self.session.execute(delete(GeneratedMedia).where(GeneratedMedia.id == media_id))


async def run_episode_illustrations_background(
    *,
    episode_id: uuid.UUID,
    work_id: uuid.UUID,
    model: Mapping[str, Any] | None = None,
    config: Any | None = None,
    llm_client: Any | None = None,
) -> None:
    """本文生成後に挿絵を起動する。失敗しても本文には影響しない。"""
    from ..memory.database import get_db_session
    from .story_studio import StoryJobExecutor

    async with await get_db_session() as session:
        executor: StoryJobExecutor | None = None
        try:
            work = await session.get(StoryWork, work_id)
            episode = await session.get(StoryEpisode, episode_id)
            if work is None or episode is None:
                return
            if not is_image_settings_enabled(work.image_settings):
                return
            executor = StoryJobExecutor(session, llm_client, config=config)
            executor._set_usage_context(work)

            async def generate_text(prompt: str) -> str:
                try:
                    return await executor._generate_text(prompt, model)
                except Exception as exc:
                    logger.warning(
                        "挿絵候補抽出: 作品モデル %s 失敗、実行中 LLM にフォールバック: %s",
                        model,
                        exc,
                    )
                    return await executor._generate_text(prompt, None)

            service = StoryIllustrationService(session, config=config)
            await service.generate_for_episode(work, episode, generate_text=generate_text)
            await session.commit()
        except Exception:
            logger.warning("挿絵バックグラウンド生成に失敗: %s", episode_id, exc_info=True)
            await session.rollback()
        finally:
            if executor is not None:
                await executor.aclose()
