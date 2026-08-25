"""TRPG Play セッションの永続化と認可。"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..memory.models.story import StoryEpisode, StoryWork
from ..memory.models.trpg_play import (
    TrpgPlayEvent,
    TrpgPlayParticipant,
    TrpgPlayPrivateState,
    TrpgPlaySession,
    TrpgPlayWhisper,
    TrpgPlayWhisperRecipient,
)
from .trpg_play_dice import roll_play_dice
from .trpg_play_gm import TrpgPlayGmService
from .trpg_play_image_service import (
    PlayImageBackgroundJob,
    TrpgPlayImageService,
    normalize_play_image_settings,
)

logger = logging.getLogger(__name__)

RECENT_EVENT_LIMIT = 50
INVITE_ALPHABET = string.ascii_uppercase + string.digits
PRIVATE_STATE_ENTRY_KEY_MAX = 128


def normalize_private_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """private state を entries 形式に正規化する。"""

    if not isinstance(state, Mapping):
        return {"entries": {}}
    raw_entries = state.get("entries")
    if not isinstance(raw_entries, Mapping):
        return {"entries": {}}
    entries: dict[str, Any] = {}
    for key, value in raw_entries.items():
        entry_key = str(key).strip()
        if not entry_key or len(entry_key) > PRIVATE_STATE_ENTRY_KEY_MAX:
            continue
        if not isinstance(value, Mapping):
            entries[entry_key] = {"value": value, "shared_with_gm": False}
            continue
        shared_with_gm = bool(value.get("shared_with_gm"))
        entries[entry_key] = {
            "value": value.get("value"),
            "shared_with_gm": shared_with_gm,
        }
    return {"entries": entries}


def filter_private_state_for_gm(state: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_private_state(state)
    visible_entries = {
        key: value
        for key, value in normalized["entries"].items()
        if isinstance(value, Mapping) and bool(value.get("shared_with_gm"))
    }
    return {"entries": visible_entries}


class TrpgPlayError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class TrpgPlayForbidden(TrpgPlayError):
    def __init__(self, message: str = "この卓へのアクセスは許可されていません"):
        super().__init__(message, status_code=403)


class TrpgPlayConflict(TrpgPlayError):
    def __init__(self, message: str):
        super().__init__(message, status_code=409)


def _new_invite_code() -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(6))


class TrpgPlayService:
    def __init__(
        self,
        session: AsyncSession,
        llm_client: Any | None = None,
        *,
        config: Any | None = None,
    ):
        self.session = session
        self.llm_client = llm_client
        self.config = config
        self._pending_image_jobs: list[PlayImageBackgroundJob] = []

    def drain_image_jobs(self) -> list[PlayImageBackgroundJob]:
        jobs = list(self._pending_image_jobs)
        self._pending_image_jobs.clear()
        return jobs

    async def _get_session(self, session_id: UUID) -> TrpgPlaySession | None:
        return await self.session.get(TrpgPlaySession, session_id)

    async def _get_work(self, work_id: UUID) -> StoryWork | None:
        return await self.session.get(StoryWork, work_id)

    async def _participants_for(self, session_id: UUID) -> list[TrpgPlayParticipant]:
        result = await self.session.execute(
            select(TrpgPlayParticipant)
            .where(
                TrpgPlayParticipant.session_id == session_id,
                TrpgPlayParticipant.left_at.is_(None),
            )
            .order_by(TrpgPlayParticipant.joined_at)
        )
        return list(result.scalars().all())

    async def _participant_for_user(
        self, session_id: UUID, user_id: UUID, *, include_left: bool = False
    ) -> TrpgPlayParticipant | None:
        query = select(TrpgPlayParticipant).where(
            TrpgPlayParticipant.session_id == session_id,
            TrpgPlayParticipant.user_id == user_id,
        )
        if not include_left:
            query = query.where(TrpgPlayParticipant.left_at.is_(None))
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def _require_participant(self, session_id: UUID, user_id: UUID) -> TrpgPlayParticipant:
        participant = await self._participant_for_user(session_id, user_id)
        if participant is None:
            raise TrpgPlayForbidden()
        return participant

    def _require_non_spectator(self, participant: TrpgPlayParticipant) -> None:
        if str(participant.role or "").strip().lower() == "spectator":
            raise TrpgPlayForbidden("観戦者はこの操作を実行できません")

    def _require_gm_or_host(
        self,
        session_row: TrpgPlaySession,
        participant: TrpgPlayParticipant,
        user_id: UUID,
        *,
        message: str = "この操作を実行できるのはホストまたは GM です",
    ) -> None:
        if participant.role not in {"gm"} and str(session_row.host_user_id) != str(user_id):
            raise TrpgPlayForbidden(message)

    def _select_lobby_host_successor(
        self,
        participants: Sequence[TrpgPlayParticipant],
        leaving_user_id: UUID,
    ) -> TrpgPlayParticipant | None:
        others = [
            item
            for item in participants
            if item.user_id is not None and str(item.user_id) != str(leaving_user_id)
        ]
        for item in others:
            if str(item.role or "").strip().lower() == "gm":
                return item
        for item in others:
            if str(item.role or "").strip().lower() != "spectator":
                return item
        return None

    async def _recent_events(
        self,
        session_id: UUID,
        *,
        limit: int = RECENT_EVENT_LIMIT,
    ) -> list[TrpgPlayEvent]:
        result = await self.session.execute(
            select(TrpgPlayEvent)
            .where(TrpgPlayEvent.session_id == session_id)
            .order_by(TrpgPlayEvent.created_at.desc())
            .limit(limit)
        )
        items = list(result.scalars().all())
        items.reverse()
        return items

    def _event_dicts(
        self,
        events: Sequence[TrpgPlayEvent],
        participants: Sequence[TrpgPlayParticipant],
    ) -> list[dict[str, Any]]:
        name_map = {str(item.id): item.display_name for item in participants}
        return [
            event.to_dict(
                actor_display_name=name_map.get(str(event.actor_participant_id or ""))
            )
            for event in events
        ]

    async def create_session(
        self,
        *,
        user_id: UUID,
        work_id: UUID,
        gm_mode: str,
        title: str | None = None,
    ) -> TrpgPlaySession:
        work = await self._get_work(work_id)
        if work is None:
            raise TrpgPlayError("作品が見つかりません", status_code=404)
        if str(work.user_id) != str(user_id):
            raise TrpgPlayForbidden("作品所有者のみ卓を作成できます")
        if str(work.kind or "").strip().lower() != "trpg":
            raise TrpgPlayConflict("TRPG 作品以外では卓を作成できません")

        normalized_mode = str(gm_mode or "human").strip().lower()
        if normalized_mode not in {"human", "ai"}:
            raise TrpgPlayError("gm_mode は human または ai です")

        session_row = TrpgPlaySession(
            work_id=work_id,
            host_user_id=user_id,
            title=(title or work.title or "TRPG卓").strip(),
            gm_mode=normalized_mode,
            status="lobby",
            invite_code=_new_invite_code(),
            snapshot={},
        )
        self.session.add(session_row)
        await self.session.flush()

        host_participant = TrpgPlayParticipant(
            session_id=session_row.id,
            user_id=user_id,
            display_name="ホスト",
            role="gm",
        )
        self.session.add(host_participant)
        await self.session.flush()
        return session_row

    async def list_sessions(self, user_id: UUID) -> list[TrpgPlaySession]:
        participant_session_ids = select(TrpgPlayParticipant.session_id).where(
            TrpgPlayParticipant.user_id == user_id,
            TrpgPlayParticipant.left_at.is_(None),
        )
        result = await self.session.execute(
            select(TrpgPlaySession)
            .where(TrpgPlaySession.id.in_(participant_session_ids))
            .order_by(TrpgPlaySession.updated_at.desc())
        )
        return list(result.scalars().all())

    async def get_session_detail(
        self,
        session_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        participant = await self._require_participant(session_id, user_id)
        participants = await self._participants_for(session_id)
        events = await self._recent_events(session_id)
        payload = session_row.to_dict(
            participants=[item.to_dict() for item in participants],
            recent_events=self._event_dicts(events, participants),
        )
        payload["viewer_participant_id"] = str(participant.id)
        return payload

    async def join_session(
        self,
        session_id: UUID,
        *,
        user_id: UUID,
        invite_code: str,
        display_name: str,
        role: str = "player",
        story_character_id: UUID | None = None,
    ) -> TrpgPlayParticipant:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        if session_row.status == "ended":
            raise TrpgPlayConflict("終了した卓には参加できません")

        code = str(invite_code or "").strip().upper()
        stored = str(session_row.invite_code or "").strip().upper()
        if not code or code != stored:
            raise TrpgPlayForbidden("招待コードが正しくありません")

        normalized_role = str(role or "player").strip().lower()
        if normalized_role not in {"gm", "player", "spectator"}:
            normalized_role = "player"
        if str(session_row.host_user_id) != str(user_id) and normalized_role == "gm":
            normalized_role = "player"

        name = str(display_name or "").strip() or "プレイヤー"

        existing = await self._participant_for_user(session_id, user_id, include_left=True)
        if existing is not None:
            if existing.left_at is None:
                return existing
            existing.left_at = None
            existing.display_name = name
            existing.role = normalized_role
            existing.story_character_id = story_character_id
            existing.joined_at = datetime.utcnow()
            session_row.updated_at = datetime.utcnow()
            await self.session.flush()
            return existing

        participant = TrpgPlayParticipant(
            session_id=session_id,
            user_id=user_id,
            display_name=name,
            role=normalized_role,
            story_character_id=story_character_id,
        )
        self.session.add(participant)
        session_row.updated_at = datetime.utcnow()
        await self.session.flush()
        return participant

    async def _append_event(
        self,
        session_row: TrpgPlaySession,
        *,
        kind: str,
        body: str,
        actor_participant_id: UUID | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> TrpgPlayEvent:
        event = TrpgPlayEvent(
            session_id=session_row.id,
            actor_participant_id=actor_participant_id,
            kind=kind,
            body=body.strip(),
            meta=dict(meta or {}),
        )
        self.session.add(event)
        session_row.updated_at = datetime.utcnow()
        await self.session.flush()
        return event

    async def _maybe_ai_gm_narration(
        self,
        session_row: TrpgPlaySession,
        *,
        trigger: str,
    ) -> TrpgPlayEvent | None:
        if str(session_row.gm_mode or "").lower() != "ai":
            return None
        if self.llm_client is None:
            await self._append_event(
                session_row,
                kind="system",
                body="AI GM は利用できません（LLM 未設定）。",
            )
            return None

        work = await self._get_work(session_row.work_id)
        if work is None:
            return None

        participants = await self._participants_for(session_row.id)
        gm_shared_private_states = await self._list_active_gm_shared_private_states(
            session_row.id
        )
        recent = await self._recent_events(session_row.id)
        gm_service = TrpgPlayGmService(
            self.session,
            self.llm_client,
            config=self.config,
        )
        narration = await gm_service.generate_narration(
            work=work,
            snapshot=session_row.snapshot or {},
            recent_events=self._event_dicts(recent, participants),
            trigger=trigger,
            gm_shared_private_states=gm_shared_private_states,
            participants=participants,
        )
        if not narration:
            return await self._append_event(
                session_row,
                kind="system",
                body="AI GM のナレーション生成に失敗しました。卓は継続します。",
            )
        return await self._append_event(
            session_row,
            kind="narration",
            body=narration,
            meta={"source": "ai_gm"},
        )

    async def _maybe_prepare_play_image(
        self,
        session_row: TrpgPlaySession,
        event: TrpgPlayEvent,
        *,
        trigger_hint: str | None = None,
        action_text: str = "",
        narration_text: str = "",
        previous_snapshot: Mapping[str, Any] | None = None,
        manual_prompt: str | None = None,
    ) -> None:
        try:
            image_service = TrpgPlayImageService(self.session, config=self.config)
            job = await image_service.prepare_for_event(
                session_row,
                event,
                trigger_hint=trigger_hint,
                action_text=action_text,
                narration_text=narration_text,
                previous_snapshot=previous_snapshot,
                manual_prompt=manual_prompt,
            )
            if job is not None:
                self._pending_image_jobs.append(job)
        except Exception:
            logger.exception("TRPG Play 画像生成でエラー（卓は継続）")

    async def start_session(self, session_id: UUID, user_id: UUID) -> TrpgPlaySession:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        participant = await self._require_participant(session_id, user_id)
        self._require_non_spectator(participant)
        self._require_gm_or_host(
            session_row,
            participant,
            user_id,
            message="開始できるのはホストまたは GM です",
        )
        if session_row.status != "lobby":
            raise TrpgPlayConflict("lobby 状態の卓のみ開始できます")

        session_row.status = "active"
        session_row.updated_at = datetime.utcnow()
        previous_snapshot = dict(session_row.snapshot or {})
        system_event = await self._append_event(
            session_row, kind="system", body="セッションを開始しました。"
        )
        narration_event = await self._maybe_ai_gm_narration(session_row, trigger="セッション開始")
        anchor_event = narration_event or system_event
        anchor_text = anchor_event.body if anchor_event.kind == "narration" else ""
        await self._maybe_prepare_play_image(
            session_row,
            anchor_event,
            trigger_hint="scene_shift",
            narration_text=anchor_text,
            previous_snapshot=previous_snapshot,
        )
        return session_row

    async def end_session(self, session_id: UUID, user_id: UUID) -> TrpgPlaySession:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        participant = await self._require_participant(session_id, user_id)
        self._require_non_spectator(participant)
        self._require_gm_or_host(
            session_row,
            participant,
            user_id,
            message="終了できるのはホストまたは GM です",
        )
        if session_row.status == "ended":
            return session_row

        session_row.status = "ended"
        session_row.ended_at = datetime.utcnow()
        session_row.updated_at = datetime.utcnow()
        await self._append_event(session_row, kind="system", body="セッションを終了しました。")
        return session_row

    async def post_action(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        kind: str,
        text: str,
    ) -> list[TrpgPlayEvent]:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        if session_row.status != "active":
            raise TrpgPlayConflict("active 状態の卓でのみ発言できます")
        participant = await self._require_participant(session_id, user_id)
        self._require_non_spectator(participant)
        normalized_kind = str(kind or "action").strip().lower()
        if normalized_kind not in {"speech", "action", "ooc"}:
            raise TrpgPlayError("kind は speech / action / ooc です")
        body = str(text or "").strip()
        if not body:
            raise TrpgPlayError("text が空です")

        previous_snapshot = dict(session_row.snapshot or {})

        if session_row.gm_mode == "human" and participant.role == "gm":
            event_kind = "gm" if normalized_kind == "speech" else normalized_kind
        else:
            event_kind = normalized_kind

        created: list[TrpgPlayEvent] = []
        action_event = await self._append_event(
            session_row,
            kind=event_kind,
            body=body,
            actor_participant_id=participant.id,
        )
        created.append(action_event)

        if session_row.gm_mode == "human" and event_kind == "gm":
            await self._maybe_prepare_play_image(
                session_row,
                action_event,
                action_text=body,
                narration_text=body,
                previous_snapshot=previous_snapshot,
            )

        if session_row.gm_mode == "ai":
            gm_event = await self._maybe_ai_gm_narration(
                session_row,
                trigger=f"{participant.display_name}: {body}",
            )
            if gm_event is not None:
                created.append(gm_event)
                await self._maybe_prepare_play_image(
                    session_row,
                    gm_event,
                    action_text=body,
                    narration_text=gm_event.body,
                    previous_snapshot=previous_snapshot,
                )
        return created

    async def roll_dice(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        expression: str,
        note: str | None = None,
    ) -> TrpgPlayEvent:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        if session_row.status != "active":
            raise TrpgPlayConflict("active 状態の卓でのみダイスを振れます")
        participant = await self._require_participant(session_id, user_id)
        self._require_non_spectator(participant)

        try:
            roll = roll_play_dice(expression)
        except ValueError as exc:
            raise TrpgPlayError(str(exc)) from exc

        label = f"{roll['expression']} → {roll['total']}"
        if note:
            label = f"{label} ({note.strip()})"
        return await self._append_event(
            session_row,
            kind="dice",
            body=label,
            actor_participant_id=participant.id,
            meta=roll,
        )

    async def list_events(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        limit: int = 100,
        before_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        await self._require_participant(session_id, user_id)
        query = select(TrpgPlayEvent).where(TrpgPlayEvent.session_id == session_id)
        if before_id is not None:
            anchor = await self.session.get(TrpgPlayEvent, before_id)
            if anchor is not None and anchor.created_at is not None:
                query = query.where(TrpgPlayEvent.created_at < anchor.created_at)
        query = query.order_by(TrpgPlayEvent.created_at.desc()).limit(min(limit, 200))
        result = await self.session.execute(query)
        events = list(result.scalars().all())
        events.reverse()
        participants = await self._participants_for(session_id)
        return self._event_dicts(events, participants)

    async def list_whispers(
        self,
        session_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        participant = await self._require_participant(session_id, user_id)
        participant_id = participant.id

        whisper_ids_for_viewer = (
            select(TrpgPlayWhisper.id)
            .join(
                TrpgPlayWhisperRecipient,
                TrpgPlayWhisperRecipient.whisper_id == TrpgPlayWhisper.id,
            )
            .where(
                TrpgPlayWhisper.session_id == session_id,
                or_(
                    TrpgPlayWhisper.sender_participant_id == participant_id,
                    TrpgPlayWhisperRecipient.participant_id == participant_id,
                ),
            )
        )
        result = await self.session.execute(
            select(TrpgPlayWhisper)
            .options(selectinload(TrpgPlayWhisper.recipients))
            .where(TrpgPlayWhisper.id.in_(whisper_ids_for_viewer))
            .order_by(TrpgPlayWhisper.created_at)
        )
        whispers = list(result.scalars().all())
        payloads: list[dict[str, Any]] = []
        for whisper in whispers:
            recipients = list(whisper.recipients or [])
            payloads.append(
                whisper.to_dict(
                    recipient_participant_ids=[str(item.participant_id) for item in recipients]
                )
            )
        return payloads

    async def post_whisper(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        body: str,
        recipient_participant_ids: Sequence[UUID],
    ) -> TrpgPlayWhisper:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        if session_row.status == "ended":
            raise TrpgPlayConflict("終了した卓では whisper できません")
        sender = await self._require_participant(session_id, user_id)
        self._require_non_spectator(sender)
        text = str(body or "").strip()
        if not text:
            raise TrpgPlayError("body が空です")

        recipient_ids = list(dict.fromkeys(recipient_participant_ids))
        if not recipient_ids:
            raise TrpgPlayError("宛先が必要です")
        if sender.id in recipient_ids:
            raise TrpgPlayError("自分自身を whisper 宛先にできません")

        participants = await self._participants_for(session_id)
        valid_ids = {item.id for item in participants}
        for recipient_id in recipient_ids:
            if recipient_id not in valid_ids:
                raise TrpgPlayError("宛先参加者が卓に存在しません")

        whisper = TrpgPlayWhisper(
            session_id=session_id,
            sender_participant_id=sender.id,
            body=text,
        )
        self.session.add(whisper)
        await self.session.flush()
        for recipient_id in recipient_ids:
            self.session.add(
                TrpgPlayWhisperRecipient(
                    whisper_id=whisper.id,
                    participant_id=recipient_id,
                )
            )
        session_row.updated_at = datetime.utcnow()
        await self.session.flush()
        return whisper

    async def patch_snapshot(
        self,
        session_id: UUID,
        user_id: UUID,
        snapshot: Mapping[str, Any],
    ) -> TrpgPlaySession:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        participant = await self._require_participant(session_id, user_id)
        self._require_non_spectator(participant)
        self._require_gm_or_host(
            session_row,
            participant,
            user_id,
            message="スナップショットを更新できるのはホストまたは GM です",
        )

        session_row.snapshot = dict(snapshot or {})
        session_row.updated_at = datetime.utcnow()
        await self.session.flush()
        return session_row

    async def patch_image_settings(
        self,
        session_id: UUID,
        user_id: UUID,
        image_settings: Mapping[str, Any],
    ) -> TrpgPlaySession:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        participant = await self._require_participant(session_id, user_id)
        self._require_non_spectator(participant)
        self._require_gm_or_host(
            session_row,
            participant,
            user_id,
            message="画像設定を変更できるのはホストまたは GM です",
        )
        if session_row.status == "ended":
            raise TrpgPlayConflict("終了した卓では画像設定を変更できません")

        merged = dict(session_row.image_settings or {})
        if isinstance(image_settings, Mapping):
            merged.update(image_settings)
        session_row.image_settings = normalize_play_image_settings(merged)
        session_row.updated_at = datetime.utcnow()
        await self.session.flush()
        return session_row

    async def generate_image_manual(
        self,
        session_id: UUID,
        user_id: UUID,
        *,
        prompt: str | None = None,
    ) -> tuple[TrpgPlayEvent, str | None]:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        if session_row.status != "active":
            raise TrpgPlayConflict("active 状態の卓でのみ画像を生成できます")
        participant = await self._require_participant(session_id, user_id)
        self._require_non_spectator(participant)

        anchor = await self._append_event(
            session_row,
            kind="system",
            body="場面画像を生成しています…",
            actor_participant_id=participant.id,
            meta={"image_request": "manual"},
        )
        pending_before = len(self._pending_image_jobs)
        await self._maybe_prepare_play_image(
            session_row,
            anchor,
            trigger_hint="manual",
            manual_prompt=prompt,
        )
        if len(self._pending_image_jobs) == pending_before:
            anchor.body = "場面画像の生成に失敗したか、設定が OFF です。"
            await self.session.flush()
        return anchor, None

    async def _private_state_for_participant(
        self,
        session_id: UUID,
        participant_id: UUID,
    ) -> TrpgPlayPrivateState | None:
        result = await self.session.execute(
            select(TrpgPlayPrivateState).where(
                TrpgPlayPrivateState.session_id == session_id,
                TrpgPlayPrivateState.participant_id == participant_id,
            )
        )
        return result.scalar_one_or_none()

    async def _get_or_create_private_state(
        self,
        session_id: UUID,
        participant_id: UUID,
    ) -> TrpgPlayPrivateState:
        existing = await self._private_state_for_participant(session_id, participant_id)
        if existing is not None:
            return existing
        row = TrpgPlayPrivateState(
            session_id=session_id,
            participant_id=participant_id,
            state={"entries": {}},
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_own_private_state(
        self,
        session_id: UUID,
        user_id: UUID,
    ) -> dict[str, Any]:
        participant = await self._require_participant(session_id, user_id)
        self._require_non_spectator(participant)
        row = await self._get_or_create_private_state(session_id, participant.id)
        payload = row.to_dict()
        payload["state"] = normalize_private_state(row.state)
        return payload

    async def patch_own_private_state(
        self,
        session_id: UUID,
        user_id: UUID,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        if session_row.status == "ended":
            raise TrpgPlayConflict("終了した卓では private state を更新できません")
        participant = await self._require_participant(session_id, user_id)
        self._require_non_spectator(participant)
        row = await self._get_or_create_private_state(session_id, participant.id)
        row.state = normalize_private_state(state)
        row.updated_at = datetime.utcnow()
        session_row.updated_at = datetime.utcnow()
        await self.session.flush()
        payload = row.to_dict()
        payload["state"] = normalize_private_state(row.state)
        return payload

    async def list_gm_visible_private_states(
        self,
        session_id: UUID,
        user_id: UUID,
    ) -> list[dict[str, Any]]:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        viewer = await self._require_participant(session_id, user_id)
        if viewer.role != "gm":
            raise TrpgPlayForbidden("GM のみ他参加者の private state を参照できます")
        return await self._list_active_gm_shared_private_states(session_id)

    async def _list_active_gm_shared_private_states(
        self,
        session_id: UUID,
    ) -> list[dict[str, Any]]:
        participants = await self._participants_for(session_id)
        name_map = {str(item.id): item.display_name for item in participants}
        active_ids = {item.id for item in participants}
        result = await self.session.execute(
            select(TrpgPlayPrivateState).where(TrpgPlayPrivateState.session_id == session_id)
        )
        payloads: list[dict[str, Any]] = []
        for row in result.scalars().all():
            if row.participant_id not in active_ids:
                continue
            filtered = filter_private_state_for_gm(row.state)
            if not filtered["entries"]:
                continue
            payloads.append(
                {
                    "participant_id": str(row.participant_id),
                    "display_name": name_map.get(str(row.participant_id)),
                    "state": filtered,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
            )
        return payloads

    async def leave_session(
        self,
        session_id: UUID,
        user_id: UUID,
    ) -> TrpgPlayParticipant:
        session_row = await self._get_session(session_id)
        if session_row is None:
            raise TrpgPlayError("卓が見つかりません", status_code=404)
        participant = await self._require_participant(session_id, user_id)

        if session_row.status == "active":
            if str(session_row.host_user_id) == str(user_id):
                raise TrpgPlayConflict("ホストはセッションを終了してから退出してください")
            active_gms = [
                item
                for item in await self._participants_for(session_id)
                if item.role == "gm"
            ]
            if participant.role == "gm" and len(active_gms) <= 1:
                raise TrpgPlayConflict(
                    "最後の GM は退出できません。セッションを終了するか別の GM を任命してください"
                )

        if session_row.status == "lobby" and str(session_row.host_user_id) == str(user_id):
            successor = self._select_lobby_host_successor(
                await self._participants_for(session_id),
                user_id,
            )
            if successor is None:
                raise TrpgPlayConflict(
                    "ホストは後継の参加者がいる場合のみ退出できます"
                )
            session_row.host_user_id = successor.user_id
            if str(successor.role or "").strip().lower() != "gm":
                successor.role = "gm"

        private_state = await self._private_state_for_participant(session_id, participant.id)
        if private_state is not None:
            await self.session.delete(private_state)

        participant.left_at = datetime.utcnow()
        session_row.updated_at = datetime.utcnow()
        await self.session.flush()
        return participant

    async def verify_episode_body_unchanged(
        self,
        work_id: UUID,
        episode_id: UUID,
        baseline_body: str | None,
    ) -> bool:
        """snapshot 更新が episode.body を書き換えていないことを検証する補助。"""

        episode = await self.session.get(StoryEpisode, episode_id)
        if episode is None or str(episode.work_id) != str(work_id):
            return False
        current = episode.body
        if baseline_body is None:
            return current is None or str(current) == ""
        return str(current or "") == str(baseline_body)


__all__ = [
    "TrpgPlayService",
    "TrpgPlayError",
    "TrpgPlayForbidden",
    "TrpgPlayConflict",
    "normalize_private_state",
    "filter_private_state_for_gm",
]
