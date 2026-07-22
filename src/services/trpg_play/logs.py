"""ログ追加・取得、開示情報、個別チャットメッセージ。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, desc

from ...memory.database import get_db_session
from ...models.ecc_models import (
    ScenarioParticipant,
    ScenarioPlaySession,
    ScenarioPlayLog,
    TRPGPrivateMessage,
    TRPGRoomDisclosure,
)
from ...utils.uuid_utils import parse_uuid, parse_uuid_strict
from ._shared import (
    DISCLOSURE_TYPES,
    DISCLOSURE_VISIBILITIES,
    GM_TARGET_ID,
    ParticipantNotFoundError,
    RoomNotFoundError,
    TRPGPlayError,
    _append_log_internal,
    _normalize_target_ids,
)
from .access import (
    _resolve_viewer_context,
    _viewer_can_see_disclosure,
    _viewer_can_see_private_message,
)


async def append_log(
    room_id: str,
    log_type: str,
    content: str,
    participant_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """任意のログをルームに追加する。"""
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    pid = parse_uuid(participant_id) if participant_id else None

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        log = await _append_log_internal(
            session, room_uid, pid, log_type, content, metadata
        )
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(log)
        return log.to_dict()


async def list_logs(
    room_id: str,
    limit: int = 200,
    before_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """ログをページング取得する（新しい順）。"""
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    async with await get_db_session() as session:
        stmt = (
            select(ScenarioPlayLog)
            .where(ScenarioPlayLog.play_session_id == room_uid)
            .order_by(desc(ScenarioPlayLog.created_at))
            .limit(limit)
        )
        if before_id:
            before_uid = parse_uuid(before_id)
            if before_uid:
                anchor = await session.get(ScenarioPlayLog, before_uid)
                if anchor:
                    stmt = stmt.where(ScenarioPlayLog.created_at < anchor.created_at)
        result = await session.execute(stmt)
        logs = result.scalars().all()
        return [log.to_dict() for log in reversed(logs)]


# ────────────────────────────────────────────
# 開示情報 / 個別チャット
# ────────────────────────────────────────────


async def list_disclosures(
    room_id: str,
    viewer_participant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        viewer, is_host = await _resolve_viewer_context(
            session, play_session, viewer_participant_id, user_id
        )
        result = await session.execute(
            select(TRPGRoomDisclosure)
            .where(TRPGRoomDisclosure.play_session_id == room_uid)
            .order_by(desc(TRPGRoomDisclosure.is_pinned), TRPGRoomDisclosure.created_at)
        )
        return [
            disclosure.to_dict()
            for disclosure in result.scalars().all()
            if _viewer_can_see_disclosure(disclosure, viewer, is_host)
        ]


async def create_disclosure(
    room_id: str,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    visibility = str(payload.get("visibility") or "public").strip().lower()
    if visibility not in DISCLOSURE_VISIBILITIES:
        raise TRPGPlayError("開示範囲が不正です", status_code=400)
    disclosure_type = str(payload.get("disclosure_type") or "handout").strip().lower()
    if disclosure_type not in DISCLOSURE_TYPES:
        disclosure_type = "note"
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or "").strip()
    image_url = str(payload.get("image_url") or "").strip()
    image_path = str(payload.get("image_path") or "").strip()
    if not title:
        raise TRPGPlayError("開示情報のタイトルは必須です", status_code=400)
    if not content and not image_url and not image_path:
        raise TRPGPlayError("本文または画像URL/パスを入力してください", status_code=400)

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        creator_id = parse_uuid(payload.get("creator_participant_id"))
        if creator_id is None:
            raise TRPGPlayError("開示情報の作成者が必要です", status_code=400)
        creator = await session.get(ScenarioParticipant, creator_id)
        if creator is None or creator.play_session_id != room_uid:
            raise ParticipantNotFoundError(str(creator_id))
        user_uid = parse_uuid(user_id) if user_id else None
        is_host = bool(user_uid and play_session.host_user_id == user_uid)
        if creator.user_id and creator.user_id != user_uid and not is_host:
            raise TRPGPlayError("この参加者として開示できません", status_code=403)
        targets = _normalize_target_ids(payload.get("target_participant_ids"))
        if visibility == "private" and not targets:
            raise TRPGPlayError("個別開示には宛先が必要です", status_code=400)

        disclosure = TRPGRoomDisclosure(
            id=uuid.uuid4(),
            play_session_id=room_uid,
            creator_participant_id=creator.id if creator else None,
            disclosure_type=disclosure_type,
            visibility=visibility,
            target_participant_ids=targets,
            title=title,
            content=content,
            image_url=image_url,
            image_path=image_path,
            tags=[
                str(tag).strip()
                for tag in (payload.get("tags") or [])
                if str(tag).strip()
            ],
            disclosure_metadata=payload.get("metadata") or {},
            is_pinned=bool(payload.get("is_pinned")),
        )
        session.add(disclosure)
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(disclosure)
        return disclosure.to_dict()


async def list_private_messages(
    room_id: str,
    viewer_participant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        viewer, is_host = await _resolve_viewer_context(
            session, play_session, viewer_participant_id, user_id
        )
        result = await session.execute(
            select(TRPGPrivateMessage)
            .where(TRPGPrivateMessage.play_session_id == room_uid)
            .order_by(desc(TRPGPrivateMessage.created_at))
            .limit(limit)
        )
        messages = [
            message
            for message in result.scalars().all()
            if _viewer_can_see_private_message(message, viewer, is_host)
        ]
        return [message.to_dict() for message in reversed(messages)]


async def send_private_message(
    room_id: str,
    payload: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    content = str(payload.get("content") or "").strip()
    if not content:
        raise TRPGPlayError("メッセージ本文は必須です", status_code=400)
    targets = _normalize_target_ids(payload.get("target_participant_ids"))
    if not targets:
        raise TRPGPlayError("個別チャットには宛先が必要です", status_code=400)
    message_type = str(payload.get("message_type") or "private").strip().lower()
    if message_type not in {"private", "gm", "mention"}:
        message_type = "private"

    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        sender_id = parse_uuid(payload.get("sender_participant_id"))
        if sender_id is None:
            raise TRPGPlayError("送信者が必要です", status_code=400)
        sender = await session.get(ScenarioParticipant, sender_id)
        if sender is None or sender.play_session_id != room_uid:
            raise ParticipantNotFoundError(str(sender_id))
        user_uid = parse_uuid(user_id) if user_id else None
        is_host = bool(user_uid and play_session.host_user_id == user_uid)
        if sender.user_id and sender.user_id != user_uid and not is_host:
            raise TRPGPlayError("この参加者として送信できません", status_code=403)

        message = TRPGPrivateMessage(
            id=uuid.uuid4(),
            play_session_id=room_uid,
            sender_participant_id=sender.id,
            sender_label=sender.display_name,
            target_participant_ids=targets,
            message_type="gm" if GM_TARGET_ID in targets else message_type,
            content=content,
            message_metadata=payload.get("metadata") or {},
        )
        session.add(message)
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(message)
        return message.to_dict()


async def append_private_message_internal(
    room_id: str,
    sender_participant_id: Optional[str],
    sender_label: str,
    target_participant_ids: List[str],
    content: str,
    message_type: str = "private",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    room_uid = parse_uuid_strict(room_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    sender_uid = parse_uuid(sender_participant_id) if sender_participant_id else None
    targets = _normalize_target_ids(target_participant_ids)
    async with await get_db_session() as session:
        play_session = await session.get(ScenarioPlaySession, room_uid)
        if play_session is None:
            raise RoomNotFoundError(room_id)
        message = TRPGPrivateMessage(
            id=uuid.uuid4(),
            play_session_id=room_uid,
            sender_participant_id=sender_uid,
            sender_label=sender_label,
            target_participant_ids=targets,
            message_type=message_type,
            content=content,
            message_metadata=metadata or {},
        )
        session.add(message)
        play_session.updated_at = datetime.utcnow()
        await session.commit()
        await session.refresh(message)
        return message.to_dict()
