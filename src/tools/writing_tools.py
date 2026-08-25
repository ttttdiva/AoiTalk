"""Story Studio の Story Team Subagent 用ツール。

本文・設定資料の正本は story_* のみとし、AI の本文変更は提案キューを通さず
期待 etag を検証した単一トランザクションで直接保存する。旧 scenario / Docs
書き戻しツールはこのモジュールから除去した。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .core import tool
from ..memory.database import get_db_session
from ..memory.models import ConversationSession
from ..memory.models.story import (
    StoryCharacter,
    StoryEpisode,
    StoryLink,
    StoryNote,
    StoryRulebook,
    StoryWork,
    StoryWorkCharacter,
    StoryWorkRulebook,
    StoryWritingSession,
)
from ..services.story_studio import (
    StoryConflictError,
    StoryEpisodeService,
    StoryRevisionService,
    StoryModelResolver,
    build_story_context,
)


# §8.8 層① の参照キー。docs_ingest_service.CLIP_INGEST_ROUTE_KEY と同じ流儀で定数化する。
WRITING_ROUTE_KEY = "model_routing.classes.writing"


def _config_get(config: Any, path: str, default: Any = None) -> Any:
    """ドット区切りでアプリ設定を辿る。"""

    current: Any = config
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return default
        current = current.get(part)
    return default if current is None else current


def _load_app_config() -> dict[str, Any]:
    """DB 保存のアプリ設定を読む。設定は config.yaml ではなく DB が正本。"""

    try:
        from ..app_config_store import load_app_config_sync

        config = load_app_config_sync()
    except Exception:  # noqa: BLE001
        return {}
    return config if isinstance(config, dict) else {}


async def _resolve_writing_model(work: StoryWork) -> dict[str, str]:
    """§8.8 の 3 層解決を story_routes と同じ材料で行う。

    層① は DB 設定の ``model_routing.classes.writing`` を読み、``inherit=true``
    ならメイン LLM 設定（``llm_provider`` / ``llm_model``）を継承する。
    """

    config = await asyncio.to_thread(_load_app_config)
    writing_class = _config_get(config, WRITING_ROUTE_KEY, {}) or {}
    provider = str(config.get("llm_provider") or "").strip()
    main_llm: dict[str, Any] = {
        "provider": provider,
        "model": str(config.get("llm_model") or "").strip(),
        "base_url": str(
            _config_get(config, f"{provider}.base_url", "")
            or config.get(f"{provider}_base_url", "")
            or ""
        ).strip(),
        "reasoning_effort": str(
            _config_get(config, f"{provider}.reasoning_effort", "") or ""
        ).strip(),
    }
    return StoryModelResolver.resolve(
        {},
        work.model_override or {},
        writing_class,
        main_llm,
    )


def _uuid(value: str | UUID | None) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _resolve_scope(
    session: Any,
    *,
    conversation_id: str,
    work_id: str = "",
    episode_id: str = "",
) -> tuple[StoryWork, StoryEpisode | None, UUID | None]:
    """会話に紐付いた StoryWritingSession と作品・章を解決する。"""

    conversation_uuid = _uuid(conversation_id)
    writing = None
    conversation = None
    if conversation_uuid is not None:
        writing = await session.scalar(
            select(StoryWritingSession).where(
                StoryWritingSession.conversation_session_id == conversation_uuid
            )
        )
        conversation = await session.get(ConversationSession, conversation_uuid)

    resolved_work_id = writing.work_id if writing is not None else _uuid(work_id)
    resolved_episode_id = writing.episode_id if writing is not None else _uuid(episode_id)
    if resolved_work_id is None:
        raise ValueError("StoryWritingSession または work_id が必要です")
    work = await session.get(StoryWork, resolved_work_id)
    if work is None or work.archived_at is not None:
        raise ValueError("作品が見つかりません")
    if conversation is not None and conversation.user_id and work.user_id != conversation.user_id:
        raise ValueError("会話から作品へアクセスできません")
    episode = None
    if resolved_episode_id is not None:
        episode = await session.get(StoryEpisode, resolved_episode_id)
        if episode is None or episode.work_id != work.id or episode.archived_at is not None:
            raise ValueError("エピソードが作品に存在しません")
    return work, episode, conversation_uuid


async def _build_context(session: Any, work: StoryWork, episode: StoryEpisode) -> str:
    episodes = await StoryEpisodeService(session).list(work)
    links = list(
        (
            await session.scalars(
                select(StoryLink).where(StoryLink.work_id == work.id)
            )
        ).all()
    )
    route_ids = []
    from ..services.story_studio import resolve_story_route, story_user_choices

    route_ids = resolve_story_route(
        work.start_episode_id,
        links,
        story_user_choices(work.ui_state),
    )
    route_map = {str(item.id): item for item in episodes}
    route = [route_map[item] for item in route_ids if item in route_map]
    joins = list(
        (
            await session.scalars(
                select(StoryWorkCharacter)
                .options(selectinload(StoryWorkCharacter.character))
                .where(StoryWorkCharacter.work_id == work.id)
            )
        ).all()
    )
    characters = [item.character for item in joins if item.character is not None]
    book_joins = list(
        (
            await session.scalars(
                select(StoryWorkRulebook).where(StoryWorkRulebook.work_id == work.id)
            )
        ).all()
    )
    books = list(
        (
            await session.scalars(
                select(StoryRulebook).where(
                    StoryRulebook.id.in_([item.rulebook_id for item in book_joins])
                )
            )
        ).all()
    ) if book_joins else []
    notes = list(
        (
            await session.scalars(
                select(StoryNote).where(StoryNote.work_id == work.id)
            )
        ).all()
    )
    model = await _resolve_writing_model(work)
    return build_story_context(
        work,
        episode,
        route,
        characters=characters,
        work_characters=joins,
        rulebooks=books,
        work_rulebooks=book_joins,
        notes=notes,
        links=links,
        model=model,
    ).prompt


@tool
async def get_story_context(
    conversation_id: str,
    work_id: str = "",
    episode_id: str = "",
) -> str:
    """作品・現在ルート・対象章の執筆コンテキストを取得する。"""

    try:
        async with await get_db_session() as session:
            work, episode, _ = await _resolve_scope(
                session,
                conversation_id=conversation_id,
                work_id=work_id,
                episode_id=episode_id,
            )
            if episode is None:
                episode = await session.scalar(
                    select(StoryEpisode)
                    .where(
                        StoryEpisode.work_id == work.id,
                        StoryEpisode.archived_at.is_(None),
                    )
                    .order_by(StoryEpisode.sort_hint, StoryEpisode.created_at)
                )
            if episode is None:
                return "作品にエピソードがありません。"
            return await _build_context(session, work, episode)
    except Exception as exc:  # noqa: BLE001
        return f"Storyコンテキスト取得エラー: {exc}"


async def _write_body(
    *,
    conversation_id: str,
    episode_id: str,
    body: str,
    expected_etag: str,
    origin: str,
    message: str,
) -> str:
    if not expected_etag:
        return "エラー: expected_etag は必須です。先に get_story_context で本文を確認してください。"
    try:
        async with await get_db_session() as session:
            work, episode, _ = await _resolve_scope(
                session,
                conversation_id=conversation_id,
                episode_id=episode_id,
            )
            if episode is None:
                return "エラー: 対象エピソードが指定されていません。"
            revision_service = StoryRevisionService(session)
            # §6.2: AI 適用直前の保険は origin='pre_ai' 固定で、
            # 未保存差分がある場合のみ積む。差分判定は create_revision 側の
            # sha dedup に委ねるため force は付けない。
            await revision_service.create_revision(
                episode,
                origin="pre_ai",
                message=f"{message}前",
                created_by="ai",
            )
            revision = await revision_service.update_body(
                episode,
                body,
                expected_etag=expected_etag,
                origin=origin,
                created_by="ai",
                message=message,
            )
            if revision is None:
                # §6.2: ai_generate / ai_edit は「常に」積む。AI が既存本文と
                # 同一の結果を返して dedup された場合も、AI 実行の記録を残す。
                revision = await revision_service.create_revision(
                    episode,
                    origin=origin,
                    message=message,
                    created_by="ai",
                    force=True,
                    body=body,
                )
            await session.commit()
            return json.dumps(
                {
                    "episode_id": str(episode.id),
                    "work_id": str(work.id),
                    "body_etag": episode.body_etag,
                    "char_count": episode.char_count,
                    "current_rev_no": episode.current_rev_no,
                    "revision": revision.to_dict() if revision else None,
                },
                ensure_ascii=False,
            )
    except StoryConflictError as exc:
        return json.dumps(
            {
                "error": "conflict",
                "current_etag": exc.episode.body_etag,
                "message": str(exc),
            },
            ensure_ascii=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"Story本文保存エラー: {exc}"


@tool
async def write_episode_body(
    conversation_id: str,
    episode_id: str,
    body: str,
    expected_etag: str,
    message: str = "AI本文生成",
) -> str:
    """本文を直接保存し、origin='ai_generate' のリビジョンを積む。"""

    return await _write_body(
        conversation_id=conversation_id,
        episode_id=episode_id,
        body=body,
        expected_etag=expected_etag,
        origin="ai_generate",
        message=message,
    )


@tool
async def revise_episode_body(
    conversation_id: str,
    episode_id: str,
    replacement: str,
    expected_etag: str,
    instruction: str = "AI本文修正",
) -> str:
    """本文を直接書き換え、提案・承認キューを介さず origin='ai_edit' を積む。"""

    return await _write_body(
        conversation_id=conversation_id,
        episode_id=episode_id,
        body=replacement,
        expected_etag=expected_etag,
        origin="ai_edit",
        message=instruction,
    )


@tool
async def add_story_note(
    conversation_id: str,
    title: str,
    content: str,
    work_id: str = "",
    ai_mode: str = "keyword",
    keywords: list[str] | None = None,
) -> str:
    """作品へ設定・資料ノートを直接追加する。"""

    try:
        async with await get_db_session() as session:
            work, _, _ = await _resolve_scope(
                session,
                conversation_id=conversation_id,
                work_id=work_id,
            )
            note = StoryNote(
                work_id=work.id,
                title=title,
                content=content,
                ai_mode=ai_mode,
                keywords=list(keywords or []),
            )
            session.add(note)
            await session.commit()
            return json.dumps(
                {"id": str(note.id), "work_id": str(work.id), "title": note.title},
                ensure_ascii=False,
            )
    except Exception as exc:  # noqa: BLE001
        return f"Storyノート保存エラー: {exc}"


@tool
async def get_character_voice(
    conversation_id: str,
    character_name: str,
    work_id: str = "",
) -> str:
    """作品に所属する人物の説明・役割・口調情報を取得する。"""

    try:
        async with await get_db_session() as session:
            work, _, _ = await _resolve_scope(
                session,
                conversation_id=conversation_id,
                work_id=work_id,
            )
            rows = list(
                (
                    await session.scalars(
                        select(StoryWorkCharacter)
                        .options(selectinload(StoryWorkCharacter.character))
                        .where(StoryWorkCharacter.work_id == work.id)
                    )
                ).all()
            )
            probe = character_name.casefold()
            for row in rows:
                character = row.character
                if character is None:
                    continue
                aliases = character.aliases if isinstance(character.aliases, list) else []
                if not (
                    probe in str(character.name or "").casefold()
                    or any(probe in str(alias).casefold() for alias in aliases)
                ):
                    continue
                details = [
                    f"## {character.name}",
                    character.description or character.summary or "説明なし",
                ]
                if row.role_note:
                    details.append(f"役割: {row.role_note}")
                return "\n".join(details)
            return f"人物「{character_name}」が作品に見つかりません。"
    except Exception as exc:  # noqa: BLE001
        return f"人物情報取得エラー: {exc}"


__all__ = [
    "add_story_note",
    "get_character_voice",
    "get_story_context",
    "revise_episode_body",
    "write_episode_body",
]
