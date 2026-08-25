"""TRPG Play の AI GM ナレーション（GMAgent は使わない）。"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models.story import (
    StoryCharacter,
    StoryEpisode,
    StoryNote,
    StoryWork,
    StoryWorkCharacter,
)
from .agent_team_v3 import agent_team_scope_active
from .story_studio import StoryJobExecutor, StoryModelResolver, _get, _sid, _text


def _config_get(config: Any, path: str, default: Any = None) -> Any:
    if config is None:
        return default
    value: Any = config
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part, default)
        else:
            getter = getattr(value, "get", None)
            if callable(getter):
                value = getter(part, default)
            else:
                return default
    return value

logger = logging.getLogger(__name__)

EPISODE_BODY_MAX_PER_CHAPTER = 6000
EPISODE_BODY_TOTAL_MAX = 24000

_GM_SYSTEM = (
    "あなたはTRPGのゲームマスター（GM）です。"
    "シナリオ設定と共有ログを読み取り、没入感あるナレーションを1本返してください。"
    "プレイヤーの行動を勝手に決定せず、情景描写・NPC反応・次に取れる行動の示唆を含めてください。"
    "メタ発言、コードフェンス、IMAGE_TRIGGER 等のマーカーは出力しないでください。"
)


async def _load_work_read_context(session: AsyncSession, work: StoryWork) -> str:
    parts: list[str] = [
        f"## 作品\n{_text(work.title)}",
    ]
    synopsis = _text(work.synopsis)
    plot = _text(work.plot)
    if synopsis:
        parts.append(synopsis)
    if plot:
        parts.append(plot)
    style = _text(work.style_guide)
    if style:
        parts.append(f"## 文体\n{style}")

    work_chars = (
        await session.execute(
            select(StoryWorkCharacter).where(StoryWorkCharacter.work_id == work.id)
        )
    ).scalars().all()
    char_ids = [_sid(item.character_id) for item in work_chars if _sid(item.character_id)]
    if char_ids:
        characters = (
            await session.execute(
                select(StoryCharacter).where(StoryCharacter.id.in_(char_ids))
            )
        ).scalars().all()
        char_lines: list[str] = []
        for character in characters:
            name = _text(character.name)
            desc = _text(character.description)
            summary = _text(character.summary)
            line = name
            if summary:
                line += f": {summary}"
            if desc:
                line += f"\n{desc}"
            char_lines.append(line)
        if char_lines:
            parts.append("## キャラクター\n" + "\n".join(char_lines))

    notes = (
        await session.execute(
            select(StoryNote).where(StoryNote.work_id == work.id).order_by(StoryNote.position)
        )
    ).scalars().all()
    note_lines = [
        f"- {_text(note.title)}: {_text(note.content)}"
        for note in notes
        if _text(note.content)
    ]
    if note_lines:
        parts.append("## メモ\n" + "\n".join(note_lines))

    episodes = (
        await session.execute(
            select(StoryEpisode)
            .where(
                StoryEpisode.work_id == work.id,
                StoryEpisode.archived_at.is_(None),
            )
            .order_by(StoryEpisode.sort_hint, StoryEpisode.created_at)
        )
    ).scalars().all()
    if episodes:
        episode_blocks: list[str] = []
        total_body_chars = 0
        truncated_tail = False
        for episode in episodes:
            title = _text(episode.title) or "章"
            plot = _text(episode.plot)
            body = _text(episode.body)
            if len(body) > EPISODE_BODY_MAX_PER_CHAPTER:
                body = body[:EPISODE_BODY_MAX_PER_CHAPTER] + "…（章内省略）"
            remaining = EPISODE_BODY_TOTAL_MAX - total_body_chars
            if remaining <= 0:
                truncated_tail = True
                break
            if len(body) > remaining:
                body = body[:remaining] + "…（全体省略）"
                truncated_tail = True
            block_lines = [f"### {title}"]
            if plot:
                block_lines.append(f"プロット: {plot}")
            if body:
                block_lines.append(body)
            episode_blocks.append("\n".join(block_lines))
            total_body_chars += len(body)
            if truncated_tail:
                break
        if truncated_tail:
            episode_blocks.append("（以降の章は省略）")
        if episode_blocks:
            parts.append("## シナリオ本文（章・read-only）\n" + "\n\n".join(episode_blocks))

    return "\n\n".join(parts).strip()


def _format_recent_events(events: Sequence[Any]) -> str:
    lines: list[str] = []
    for event in events:
        kind = _text(_get(event, "kind"))
        body = _text(_get(event, "body"))
        actor = _text(_get(event, "actor_display_name"))
        prefix = f"[{kind}]"
        if actor:
            prefix += f" {actor}:"
        lines.append(f"{prefix} {body}".strip())
    return "\n".join(lines).strip()


def _format_participants(participants: Sequence[Any]) -> str:
    lines: list[str] = []
    for item in participants:
        name = _text(_get(item, "display_name"))
        role = _text(_get(item, "role"))
        if not name:
            continue
        if role:
            lines.append(f"- {name} ({role})")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


def _format_gm_shared_private_states(states: Sequence[Any]) -> str:
    blocks: list[str] = []
    for item in states:
        if not isinstance(item, Mapping):
            continue
        name = _text(item.get("display_name")) or "参加者"
        state = item.get("state")
        entries: Mapping[str, Any] | None = None
        if isinstance(state, Mapping):
            raw_entries = state.get("entries")
            if isinstance(raw_entries, Mapping):
                entries = raw_entries
        if not entries:
            continue
        entry_lines = []
        for key, value in entries.items():
            shown = value.get("value") if isinstance(value, Mapping) else value
            entry_lines.append(f"  - {key}: {shown}")
        blocks.append(f"- {name}\n" + "\n".join(entry_lines))
    return "\n".join(blocks)


class TrpgPlayGmService:
    def __init__(
        self,
        session: AsyncSession,
        llm_client: Any,
        *,
        config: Any | None = None,
    ):
        self.session = session
        self.llm_client = llm_client
        self.config = config
        self._executor = StoryJobExecutor(session, llm_client, config=config)

    async def generate_narration(
        self,
        *,
        work: StoryWork,
        snapshot: Mapping[str, Any] | None,
        recent_events: Sequence[Any],
        trigger: str,
        gm_shared_private_states: Sequence[Mapping[str, Any]] | None = None,
        participants: Sequence[Any] | None = None,
    ) -> str | None:
        """Agent Team の trpg context でモデルを解決し、ナレーションを1本生成する。"""

        scope = agent_team_scope_active(self.config, trpg_context=True)
        active_teams = scope.get("active_team_ids") or []
        logger.debug("TRPG GM scope active teams: %s", active_teams)

        try:
            work_context = await _load_work_read_context(self.session, work)
            log_text = _format_recent_events(recent_events)
            roster_text = _format_participants(participants or ())
            shared_private_text = _format_gm_shared_private_states(
                gm_shared_private_states or ()
            )
            snapshot_text = ""
            if snapshot:
                snapshot_text = "\n".join(
                    f"{key}: {value}" for key, value in snapshot.items()
                )
            prompt_parts = [_GM_SYSTEM, work_context]
            if roster_text:
                prompt_parts.append(f"## 参加者\n{roster_text}")
            if snapshot_text:
                prompt_parts.append(f"## 共有スナップショット\n{snapshot_text}")
            if shared_private_text:
                prompt_parts.append(f"## GM共有の非公開状態\n{shared_private_text}")
            if log_text:
                prompt_parts.append(f"## 直近の共有ログ\n{log_text}")
            prompt_parts.append(f"## トリガー\n{trigger.strip()}")
            prompt_parts.append("上記を踏まえ、GMナレーション本文だけを返してください。")
            prompt = "\n\n".join(part for part in prompt_parts if part).strip()

            writing = _config_get(self.config, "model_routing.classes.writing", {}) or {}
            model = StoryModelResolver.resolve(
                None,
                work.model_override or {},
                writing,
                {},
            )
            self._executor._set_usage_context(work)
            text = await self._executor._generate_text(prompt, model)
            return text.strip() if text else None
        except Exception:
            logger.exception("TRPG AI GM ナレーション生成に失敗しました")
            return None
        finally:
            await self._executor.aclose()


__all__ = ["TrpgPlayGmService"]
