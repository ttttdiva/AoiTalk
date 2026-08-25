"""Story Studio のフィールド単位 AI 編集支援。"""

from __future__ import annotations

from typing import Any, Literal, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..memory.models.story import (
    StoryCharacter,
    StoryEpisode,
    StoryNote,
    StoryRulebook,
    StoryWork,
    StoryWorkCharacter,
)
from .story_studio import (
    StoryJobExecutor,
    StoryModelResolver,
    StoryNotFoundError,
    StoryWorkService,
    _get,
    _sid,
    _text,
)

StoryAssistFieldKind = Literal[
    "episode_body",
    "episode_plot",
    "work_plot",
    "character_description",
    "character_summary",
    "character_role_note",
    "character_notes",
    "rulebook",
    "world_note",
]


def _minimal_work_context(work: StoryWork) -> str:
    parts = [f"## 作品\n{_text(_get(work, 'title'))}"]
    synopsis = _text(_get(work, "synopsis"))
    if synopsis:
        parts.append(synopsis)
    return "\n".join(parts).strip()


_SELECTION_REPLACE_INSTRUCTION = (
    "選択範囲だけを修正指示に従って書き直し、置換用テキストだけを返してください。"
    "前置き、コードフェンス、解説は不要です。"
)


def _assist_mode_instruction(field_kind: StoryAssistFieldKind, *, has_selection: bool) -> str:
    if has_selection:
        return _SELECTION_REPLACE_INSTRUCTION
    if field_kind == "episode_body":
        return "本文全体を修正指示に従って書き直し、提案本文だけを返してください。"
    if field_kind in {"episode_plot", "work_plot"}:
        return "プロットを修正指示に従って書き直し、提案テキストだけを返してください。"
    if field_kind in {"character_description", "character_summary", "character_role_note"}:
        return "対象フィールドを修正指示に従って書き直し、提案テキストだけを返してください。"
    if field_kind == "character_notes":
        return (
            "ユーザーが明示的に渡した非公開メモを修正指示に従って書き直し、"
            "提案テキストだけを返してください。"
        )
    if field_kind in {"rulebook", "world_note"}:
        return "対象本文を修正指示に従って書き直し、提案テキストだけを返してください。"
    return "修正指示に従って提案テキストだけを返してください。"


class StoryAssistService:
    def __init__(self, session: AsyncSession, llm_client: Any, *, config: Any | None = None):
        self.session = session
        self.executor = StoryJobExecutor(session, llm_client, config=config)

    async def propose(
        self,
        *,
        user_id: UUID,
        work_id: UUID | None,
        field_kind: StoryAssistFieldKind,
        current_text: str,
        instruction: str,
        model: Mapping[str, Any] | None = None,
        episode_id: UUID | None = None,
        character_id: UUID | None = None,
        rulebook_id: UUID | None = None,
        note_id: UUID | None = None,
        selection_start: int | None = None,
        selection_end: int | None = None,
        include_private_notes: bool = False,
    ) -> str:
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("修正指示が空です")

        has_selection = (
            selection_start is not None
            and selection_end is not None
            and selection_start < selection_end
        )
        if field_kind == "character_notes" and not include_private_notes:
            raise ValueError("非公開メモを AI に渡すには確認が必要です")

        work: StoryWork | None = None
        if work_id is not None:
            work = await StoryWorkService(self.session).get(work_id, user_id)

        context_sections: list[str] = []
        target_label = "対象テキスト"
        target_text = current_text

        if field_kind == "episode_body" and work is not None and episode_id is not None:
            episode = await self._owner_episode(episode_id, user_id)
            if str(episode.work_id) != str(work.id):
                raise StoryNotFoundError("エピソードが作品に属していません")
            story_context = await self.executor._context_for(work, episode, model or {})
            context_sections.append(story_context.prompt)
            target_label = "章本文"
        elif field_kind == "episode_plot" and work is not None and episode_id is not None:
            episode = await self._owner_episode(episode_id, user_id)
            context_sections.append(_minimal_work_context(work))
            plot = _text(_get(episode, "plot"))
            if plot and plot != current_text:
                context_sections.append(f"## 参考: 既存章プロット\n{plot}")
            target_label = "章プロット"
        elif field_kind == "work_plot" and work is not None:
            context_sections.append(_minimal_work_context(work))
            style = _text(_get(work, "style_guide"))
            if style:
                context_sections.append(f"## 文体・執筆指示\n{style}")
            target_label = "全体プロット"
        elif field_kind in {"character_description", "character_summary", "character_role_note", "character_notes"}:
            if character_id is not None:
                character = await self._owner_character(character_id, user_id)
                context_sections.append(f"## 人物\n{_text(_get(character, 'name'))}")
                if field_kind == "character_role_note" and work is not None:
                    join = await self.session.scalar(
                        select(StoryWorkCharacter).where(
                            StoryWorkCharacter.work_id == work.id,
                            StoryWorkCharacter.character_id == character.id,
                        )
                    )
                    role = _text(_get(join, "role_note"))
                    if role and role != current_text:
                        context_sections.append(f"## 参考: 既存の役割メモ\n{role}")
            if work is not None:
                context_sections.append(_minimal_work_context(work))
            labels = {
                "character_description": "人物説明",
                "character_summary": "一言サマリ",
                "character_role_note": "この作品での役割",
                "character_notes": "非公開メモ",
            }
            target_label = labels[field_kind]
        elif field_kind == "rulebook":
            if rulebook_id is not None:
                rulebook = await self._owner_rulebook(rulebook_id, user_id)
                context_sections.append(f"## ルール: {_text(_get(rulebook, 'name'))}")
            if work is not None:
                context_sections.append(_minimal_work_context(work))
            target_label = "ルール本文"
        elif field_kind == "world_note":
            if note_id is not None:
                note = await self._owner_note(note_id, user_id)
                context_sections.append(f"## 設定資料: {_text(_get(note, 'title'))}")
            if work is not None:
                context_sections.append(_minimal_work_context(work))
            target_label = "設定資料本文"
        elif work is not None:
            context_sections.append(_minimal_work_context(work))

        if has_selection:
            target_text = current_text[selection_start:selection_end]

        mode = _assist_mode_instruction(field_kind, has_selection=has_selection)
        prompt_parts = []
        if context_sections:
            prompt_parts.append("\n\n".join(section for section in context_sections if section))
        prompt_parts.append(
            f"\n\n## 修正対象（{target_label}）\n{target_text or '（空）'}"
            f"\n\n## 修正指示\n{instruction}"
            f"\n\n## 出力要件\n{mode}"
        )
        prompt = "\n".join(part for part in prompt_parts if part).strip()

        if work is not None:
            self.executor._set_usage_context(work)
        resolved_model = model or {}
        if work is not None and not resolved_model:
            resolved_model = StoryModelResolver.resolve(
                {},
                work.model_override or {},
                {},
                {},
            )
        proposal = await self.executor._generate_text(prompt, resolved_model)
        return proposal.strip()

    async def _owner_episode(self, episode_id: UUID | None, user_id: UUID) -> StoryEpisode:
        if episode_id is None:
            raise StoryNotFoundError("エピソードが指定されていません")
        from .story_studio import StoryEpisodeService

        return await StoryEpisodeService(self.session).get(episode_id, user_id)

    async def _owner_character(self, character_id: UUID | None, user_id: UUID) -> StoryCharacter:
        if character_id is None:
            raise StoryNotFoundError("人物が指定されていません")
        character = await self.session.scalar(
            select(StoryCharacter).where(
                StoryCharacter.id == character_id,
                StoryCharacter.user_id == user_id,
            )
        )
        if character is None:
            raise StoryNotFoundError("人物が見つかりません")
        return character

    async def _owner_rulebook(self, rulebook_id: UUID | None, user_id: UUID) -> StoryRulebook:
        if rulebook_id is None:
            raise StoryNotFoundError("ルールブックが指定されていません")
        rulebook = await self.session.scalar(
            select(StoryRulebook).where(
                StoryRulebook.id == rulebook_id,
                StoryRulebook.user_id == user_id,
            )
        )
        if rulebook is None:
            raise StoryNotFoundError("ルールブックが見つかりません")
        return rulebook

    async def _owner_note(self, note_id: UUID | None, user_id: UUID) -> StoryNote:
        if note_id is None:
            raise StoryNotFoundError("設定資料が指定されていません")
        note = await self.session.scalar(select(StoryNote).where(StoryNote.id == note_id))
        if note is None:
            raise StoryNotFoundError("設定資料が見つかりません")
        work = await self.session.get(StoryWork, note.work_id)
        if work is None or str(work.user_id) != str(user_id):
            raise StoryNotFoundError("設定資料が見つかりません")
        return note
