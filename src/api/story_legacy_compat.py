"""Story Studio の読み取り専用 legacy / mobile 互換射影。

このモジュールは ``story_*`` を正本として旧 mobile sync のレスポンス形状へ
変換するだけで、旧テーブルまたは Docs へ書き戻さない。mobile 改修時に削除する
前提の互換層である。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.models.story import (
    StoryCharacter,
    StoryEpisode,
    StoryWork,
    StoryWorkCharacter,
)


LEGACY_STORY_TABLES = frozenset(
    {
        "scenarios",
        "scenario_episodes",
        "scenario_scenes",
        "scenario_characters",
    }
)
LEGACY_STORY_PULL_LIMITS = {table: 5000 for table in LEGACY_STORY_TABLES}
_LEGACY_CHARACTER_NAMESPACE = UUID("5e7ecb3f-ec35-4d9d-9582-5a2ffb9a4d3f")


class LegacyStoryDetailResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: UUID
    title: str
    scenario_kind: str
    episodes: list[dict[str, Any]] = Field(default_factory=list)
    scenes: list[dict[str, Any]] = Field(default_factory=list)
    characters: list[dict[str, Any]] = Field(default_factory=list)
    canon: list[Any] = Field(default_factory=list)


class LegacyCanonResponse(BaseModel):
    entries: list[Any] = Field(default_factory=list)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _changed_filter(model: Any, since: datetime | None):
    if since is None:
        return None
    return or_(model.updated_at > since, model.archived_at > since)


def _legacy_character_id(work_id: UUID, character_id: UUID) -> UUID:
    """旧の所属行 UUID を安定して再現する（DB には保存しない）。"""

    return uuid5(_LEGACY_CHARACTER_NAMESPACE, f"{work_id}:{character_id}")


def _project_work(work: StoryWork) -> dict[str, Any]:
    """StoryWork を旧 Scenario DTO へ写像する。"""

    return {
        "id": str(work.id),
        "title": work.title,
        "scenario_kind": "trpg" if work.kind == "trpg" else "writing",
        "ruleset": "",
        "description": work.synopsis or "",
        "genre": "",
        "perspective": "third_person",
        "setting": work.plot or "",
        "opening_text": "",
        "gm_instructions": "",
        "tags": [],
        "cover_image_path": "",
        "knowledge_node_id": None,
        "is_published": work.status == "published",
        "created_by": str(work.user_id),
        "voice_tone": work.style_guide or "",
        "voice_tense_rules": "",
        "voice_vocabulary_register": "",
        "voice_banned_expressions": [],
        "voice_example_passages": "",
        "created_at": _iso(work.created_at),
        "updated_at": _iso(work.updated_at),
    }


def _project_episode(episode: StoryEpisode) -> dict[str, Any]:
    """StoryEpisode を旧 ScenarioEpisode DTO へ写像する。"""

    summary = episode.summary or ""
    return {
        "id": str(episode.id),
        "scenario_id": str(episode.work_id),
        "title": episode.title,
        "synopsis_sentence": summary,
        "one_line_summary": summary,
        "synopsis_paragraph": summary,
        "paragraph_summary": summary,
        "synopsis_full": summary,
        "full_summary": summary,
        "beat_sheet": [],
        "status": episode.status or "draft",
        "sort_order": int(episode.sort_hint or 0),
        "knowledge_node_id": None,
        "created_at": _iso(episode.created_at),
        "updated_at": _iso(episode.updated_at),
    }


def _project_scene(episode: StoryEpisode) -> dict[str, Any]:
    """本文章を旧 mobile の単一 scene DTO として射影する。"""

    body = episode.body or ""
    return {
        "id": str(episode.id),
        "scenario_id": str(episode.work_id),
        "episode_id": str(episode.id),
        "title": episode.title,
        "description": episode.plot or "",
        "scene_type": "normal",
        "gm_instructions": "",
        "image_prompt": "",
        "transitions": [],
        "sort_order": int(episode.sort_hint or 0),
        "order_index": int(episode.sort_hint or 0),
        "content": body,
        "body": body,
        "content_versions": [],
        "word_count": len(body),
        "status": episode.status or "draft",
        "state_snapshot": {},
        "knowledge_node_id": None,
    }


def _project_character(link: StoryWorkCharacter) -> dict[str, Any]:
    character = link.character
    if character is None:
        return {}
    return {
        "id": str(_legacy_character_id(link.work_id, link.character_id)),
        "scenario_id": str(link.work_id),
        "character_id": str(link.character_id),
        "role": "npc",
        "name": character.name,
        "description": character.description or character.summary or "",
        "personality_override": link.role_note or "",
        "appearance_tags_override": "",
        "sort_order": int(link.position or 0),
        "backstory": "",
        "psychology": "",
        "speech_patterns": "",
        "speech_pattern": "",
        "relationships": "[]",
        "relationships_data": [],
        "character_arc": "",
        "arc": "",
        "importance": 0,
        "example_dialogues": "",
        "dialogue_samples": "",
        "trpg_ruleset": "",
        "trpg_pc_state": {},
    }


def _split_changes(
    rows: list[tuple[dict[str, Any], bool]],
    *,
    authoritative_ids: list[str],
) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    tombstones: list[dict[str, Any]] = []
    for payload, archived in rows:
        if archived:
            tombstones.append(
                {"id": payload["id"], "deleted_at": payload.get("archived_at")}
            )
        else:
            changes.append(payload)
    return {
        "changes": changes,
        "tombstones": tombstones,
        "cursor": None,
        "authoritative_ids": authoritative_ids,
    }


async def pull_story_table(
    table: str,
    session: AsyncSession,
    *,
    user_id: UUID,
    since: datetime | None = None,
    limit: int = 5000,
) -> dict[str, Any]:
    """旧 sync table 名に対応する story 射影を返す。GET 専用で commit しない。"""

    if table not in LEGACY_STORY_TABLES:
        return {"changes": [], "tombstones": [], "cursor": None}

    if table == "scenarios":
        stmt = select(StoryWork).where(StoryWork.user_id == user_id)
        changed = _changed_filter(StoryWork, since)
        if changed is not None:
            stmt = stmt.where(changed)
        rows = list(
            (
                await session.scalars(
                    stmt.order_by(StoryWork.updated_at.desc()).limit(limit)
                )
            ).all()
        )
        active = list(
            (
                await session.scalars(
                    select(StoryWork.id).where(
                        StoryWork.user_id == user_id,
                        StoryWork.archived_at.is_(None),
                    )
                )
            ).all()
        )
        return _split_changes(
            [(_project_work(row), row.archived_at is not None) for row in rows],
            authoritative_ids=[str(item) for item in active],
        )

    if table in {"scenario_episodes", "scenario_scenes"}:
        stmt = (
            select(StoryEpisode)
            .join(StoryWork, StoryWork.id == StoryEpisode.work_id)
            .where(StoryWork.user_id == user_id)
        )
        changed = _changed_filter(StoryEpisode, since)
        if changed is not None:
            stmt = stmt.where(changed)
        rows = list(
            (
                await session.scalars(
                    stmt.order_by(StoryEpisode.updated_at.desc()).limit(limit)
                )
            ).all()
        )
        active = list(
            (
                await session.scalars(
                    select(StoryEpisode.id)
                    .join(StoryWork, StoryWork.id == StoryEpisode.work_id)
                    .where(
                        StoryWork.user_id == user_id,
                        StoryEpisode.archived_at.is_(None),
                    )
                )
            ).all()
        )
        projector = _project_scene if table == "scenario_scenes" else _project_episode
        return _split_changes(
            [(projector(row), row.archived_at is not None) for row in rows],
            authoritative_ids=[str(item) for item in active],
        )

    stmt = (
        select(StoryWorkCharacter)
        .options(selectinload(StoryWorkCharacter.character))
        .join(StoryWork, StoryWork.id == StoryWorkCharacter.work_id)
        .join(StoryCharacter, StoryCharacter.id == StoryWorkCharacter.character_id)
        .where(
            StoryWork.user_id == user_id,
            StoryWork.archived_at.is_(None),
            StoryCharacter.archived_at.is_(None),
        )
        .order_by(StoryWorkCharacter.position, StoryCharacter.name)
        .limit(limit)
    )
    rows = list((await session.scalars(stmt)).all())
    active = [str(_legacy_character_id(row.work_id, row.character_id)) for row in rows]
    return _split_changes(
        [(_project_character(row), False) for row in rows if row.character is not None],
        authoritative_ids=active,
    )


async def get_story_legacy_detail(
    scenario_id: UUID,
    session: AsyncSession,
    *,
    user_id: UUID,
) -> dict[str, Any]:
    """旧 GET /scenarios/{id} 相当の読み取り専用詳細。"""

    work = await session.scalar(
        select(StoryWork).where(
            StoryWork.id == scenario_id,
            StoryWork.user_id == user_id,
            StoryWork.archived_at.is_(None),
        )
    )
    if work is None:
        raise HTTPException(status_code=404, detail="シナリオが見つかりません")
    episodes = list(
        (
            await session.scalars(
                select(StoryEpisode)
                .where(
                    StoryEpisode.work_id == work.id,
                    StoryEpisode.archived_at.is_(None),
                )
                .order_by(StoryEpisode.sort_hint, StoryEpisode.created_at)
            )
        ).all()
    )
    links = list(
        (
            await session.scalars(
                select(StoryWorkCharacter)
                .options(selectinload(StoryWorkCharacter.character))
                .join(StoryCharacter, StoryCharacter.id == StoryWorkCharacter.character_id)
                .where(
                    StoryWorkCharacter.work_id == work.id,
                    StoryCharacter.archived_at.is_(None),
                )
                .order_by(StoryWorkCharacter.position, StoryCharacter.name)
            )
        ).all()
    )
    return {
        **_project_work(work),
        "episodes": [_project_episode(row) for row in episodes],
        "scenes": [_project_scene(row) for row in episodes],
        "characters": [_project_character(row) for row in links if row.character is not None],
        "canon": [],
    }


async def list_story_legacy_canon(
    scenario_id: UUID,
    session: AsyncSession,
    *,
    user_id: UUID,
) -> dict[str, list[Any]]:
    """canon は story_notes への逆投影を行わず、空配列を 200 で返す。"""

    work = await session.scalar(
        select(StoryWork.id).where(
            StoryWork.id == scenario_id,
            StoryWork.user_id == user_id,
            StoryWork.archived_at.is_(None),
        )
    )
    if work is None:
        raise HTTPException(status_code=404, detail="シナリオが見つかりません")
    return {"entries": []}


#: mobile が実際に叩くパス（`mobile/src/lib/scenario-api.ts` L39 / L252）。
#: `src/api/scenario_routes.py` は削除済みなので、ここで同じパスを再提供しないと
#: モバイルの詳細画面が 404 で壊れる（§11.9）。**GET のみ**。
MOBILE_DETAIL_PATH = "/api/scenarios/{scenario_id}"
MOBILE_CANON_PATH = "/api/scenarios/{scenario_id}/canon"
#: frontend の型生成（`frontend/src/lib/story/api.ts`）が参照している別名パス。
#: mobile 用の実パスと同じハンドラを共有する。
LEGACY_DETAIL_PATH = "/api/story/legacy/scenarios/{scenario_id}"
LEGACY_CANON_PATH = "/api/story/legacy/scenarios/{scenario_id}/canon"


def create_story_legacy_compat_router(
    *,
    get_db_manager: Any,
    get_user_from_request: Any,
    require_auth_dependency: Any,
) -> APIRouter:
    """旧 scenario 詳細の代替 GET を mobile の実パスで公開する。

    ``prefix`` を持たせず、mobile が叩く ``/api/scenarios/...`` と、frontend の
    生成型が参照する ``/api/story/legacy/scenarios/...`` の両方へ同じハンドラを
    載せる。書き込み系は提供しない（モバイルは読み取り専用に格下げ、§11.9）。
    """

    router = APIRouter(tags=["story-legacy-compat"])

    async def current_user_id(request: Request) -> UUID:
        value = get_user_from_request(request)
        if hasattr(value, "__await__"):
            value = await value
        raw = value.get("id") if isinstance(value, Mapping) else value
        try:
            return UUID(str(raw))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="ユーザーIDが不正です") from exc

    async def open_session() -> AsyncSession:
        return await get_db_manager().get_session()

    @router.get(
        MOBILE_DETAIL_PATH,
        response_model=LegacyStoryDetailResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    @router.get(
        LEGACY_DETAIL_PATH,
        response_model=LegacyStoryDetailResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def get_legacy_scenario(scenario_id: UUID, request: Request):
        session = await open_session()
        try:
            return await get_story_legacy_detail(
                scenario_id,
                session,
                user_id=await current_user_id(request),
            )
        finally:
            await session.close()

    @router.get(
        MOBILE_CANON_PATH,
        response_model=LegacyCanonResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    @router.get(
        LEGACY_CANON_PATH,
        response_model=LegacyCanonResponse,
        dependencies=[Depends(require_auth_dependency)],
    )
    async def get_legacy_canon(scenario_id: UUID, request: Request):
        session = await open_session()
        try:
            return await list_story_legacy_canon(
                scenario_id,
                session,
                user_id=await current_user_id(request),
            )
        finally:
            await session.close()

    return router


# 読み取り側の呼び出し名を明示して、sync 以外の backend 経路も同じ射影を使えるようにする。
project_story_table = pull_story_table


__all__ = [
    "LEGACY_STORY_TABLES",
    "LEGACY_STORY_PULL_LIMITS",
    "create_story_legacy_compat_router",
    "get_story_legacy_detail",
    "list_story_legacy_canon",
    "project_story_table",
    "pull_story_table",
]
