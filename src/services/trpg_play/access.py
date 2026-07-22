"""ルーム/参加者に対するアクセス制御と閲覧範囲判定。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import select

from ...memory.database import get_db_session
from ...models.ecc_models import (
    ScenarioParticipant,
    ScenarioPlaySession,
    TRPGPrivateMessage,
    TRPGRoomDisclosure,
)
from ...utils.uuid_utils import parse_uuid, parse_uuid_strict
from ._shared import (
    GM_TARGET_ID,
    ParticipantNotFoundError,
    RoomNotFoundError,
    TRPGPlayError,
    _participant_id_str,
    _participant_is_gm,
    _resolve_play_session,
)


def _viewer_can_see_disclosure(
    disclosure: TRPGRoomDisclosure,
    viewer_participant: Optional[ScenarioParticipant] = None,
    is_host: bool = False,
) -> bool:
    if is_host:
        return True
    visibility = str(getattr(disclosure, "visibility", "") or "public").lower()
    if visibility == "public":
        return True
    viewer_id = _participant_id_str(viewer_participant)
    if viewer_id and str(getattr(disclosure, "creator_participant_id", "") or "") == viewer_id:
        return True
    targets = {str(item) for item in (disclosure.target_participant_ids or [])}
    if viewer_id and viewer_id in targets:
        return True
    if GM_TARGET_ID in targets and _participant_is_gm(viewer_participant):
        return True
    if visibility == "gm" and _participant_is_gm(viewer_participant):
        return True
    return False


def _viewer_can_see_private_message(
    message: TRPGPrivateMessage,
    viewer_participant: Optional[ScenarioParticipant] = None,
    is_host: bool = False,
) -> bool:
    if is_host:
        return True
    viewer_id = _participant_id_str(viewer_participant)
    if viewer_id and str(getattr(message, "sender_participant_id", "") or "") == viewer_id:
        return True
    targets = {str(item) for item in (message.target_participant_ids or [])}
    if viewer_id and viewer_id in targets:
        return True
    if GM_TARGET_ID in targets and _participant_is_gm(viewer_participant):
        return True
    return False


async def _resolve_viewer_context(
    session,
    play_session: ScenarioPlaySession,
    viewer_participant_id: Optional[str],
    user_id: Optional[str],
) -> tuple[Optional[ScenarioParticipant], bool]:
    user_uid = parse_uuid(user_id) if user_id else None
    is_host = bool(user_uid and play_session.host_user_id == user_uid)
    participant: Optional[ScenarioParticipant] = None

    if viewer_participant_id:
        pid = parse_uuid_strict(viewer_participant_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
        participant = await session.get(ScenarioParticipant, pid)
        if participant is None or participant.play_session_id != play_session.id:
            raise ParticipantNotFoundError(viewer_participant_id)
        if participant.user_id and participant.user_id != user_uid and not is_host:
            raise TRPGPlayError("この参加者の秘匿情報は参照できません", status_code=403)
        if participant.user_id and user_uid is None:
            raise TRPGPlayError("認証が必要です", status_code=401)
        return participant, is_host

    if user_uid:
        result = await session.execute(
            select(ScenarioParticipant)
            .where(
                ScenarioParticipant.play_session_id == play_session.id,
                ScenarioParticipant.user_id == user_uid,
                ScenarioParticipant.is_active_participant.is_(True),
            )
            .order_by(ScenarioParticipant.seat_index)
        )
        participant = result.scalars().first()
    return participant, is_host


async def require_room_view_access(
    room_id_or_code: str,
    user_id: Optional[str],
    invite_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Ensure the authenticated user can view a room snapshot/log stream.

    A private room is visible to its host, active participants, or a user who
    presents the exact room invite code. Public rooms are visible to any
    authenticated user.
    """
    if not user_id:
        raise TRPGPlayError("認証が必要です", status_code=401)
    user_uid = parse_uuid(user_id)
    if user_uid is None:
        raise TRPGPlayError("認証ユーザーIDが不正です", status_code=401)

    async with await get_db_session() as session:
        play_session = await _resolve_play_session(session, room_id_or_code)
        invite = str(invite_code or "").strip().upper()
        code = str(play_session.room_code or "").strip().upper()
        invited = bool(invite and code and invite == code)
        is_host = bool(play_session.host_user_id == user_uid)

        participant_result = await session.execute(
            select(ScenarioParticipant).where(
                ScenarioParticipant.play_session_id == play_session.id,
                ScenarioParticipant.user_id == user_uid,
                ScenarioParticipant.is_active_participant.is_(True),
            )
        )
        participant = participant_result.scalars().first()
        if invited or is_host or participant or bool(play_session.is_public):
            return {
                "room_id": str(play_session.id),
                "room_code": play_session.room_code,
                "is_public": bool(play_session.is_public),
                "is_host": is_host,
                "is_participant": bool(participant),
                "participant_id": str(participant.id) if participant else None,
                "invited": invited,
            }
        raise TRPGPlayError("このルームを閲覧できません", status_code=403)


async def require_room_participation_access(
    room_id_or_code: str,
    user_id: Optional[str],
    invite_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Ensure a user can interact with a room as host or active participant."""
    # 後方互換 re-export シム（src.services.trpg_play_service）上の
    # require_room_view_access 差し替え（テストの monkeypatch 等）を尊重するため、
    # 実行時にシムモジュールの属性経由で解決する。元の単一ファイル時代の
    # 「同一モジュール内参照」の振る舞いを保存する。
    import sys

    shim = sys.modules.get("src.services.trpg_play_service")
    view_access = getattr(shim, "require_room_view_access", None) if shim else None
    if view_access is None:
        view_access = require_room_view_access
    access = await view_access(room_id_or_code, user_id, invite_code)
    if (
        access.get("is_host")
        or access.get("is_participant")
        or access.get("invited")
        or access.get("is_public")
    ):
        return access
    raise TRPGPlayError("このルームでは操作できません", status_code=403)


async def require_room_gm_access(
    room_id_or_code: str,
    user_id: Optional[str],
) -> Dict[str, Any]:
    """Ensure a user is the room host or an active GM participant."""
    if not user_id:
        raise TRPGPlayError("認証が必要です", status_code=401)
    user_uid = parse_uuid(user_id)
    if user_uid is None:
        raise TRPGPlayError("認証ユーザーIDが不正です", status_code=401)

    async with await get_db_session() as session:
        play_session = await _resolve_play_session(session, room_id_or_code)
        if play_session.host_user_id == user_uid:
            return {"room_id": str(play_session.id), "is_host": True, "is_gm": True}
        gm_result = await session.execute(
            select(ScenarioParticipant).where(
                ScenarioParticipant.play_session_id == play_session.id,
                ScenarioParticipant.user_id == user_uid,
                ScenarioParticipant.role == "gm",
                ScenarioParticipant.is_active_participant.is_(True),
            )
        )
        gm = gm_result.scalars().first()
        if gm:
            return {
                "room_id": str(play_session.id),
                "is_host": False,
                "is_gm": True,
                "participant_id": str(gm.id),
            }
        raise TRPGPlayError("GMまたはホストのみ操作できます", status_code=403)


async def require_participant_write_access(
    participant_id: str,
    user_id: Optional[str],
    allow_gm: bool = True,
) -> Dict[str, Any]:
    """Ensure a user can write as or update a participant."""
    if not user_id:
        raise TRPGPlayError("認証が必要です", status_code=401)
    participant_uid = parse_uuid_strict(participant_id, lambda v: TRPGPlayError(f"無効なUUID形式です: {v}"))
    user_uid = parse_uuid(user_id)
    if user_uid is None:
        raise TRPGPlayError("認証ユーザーIDが不正です", status_code=401)

    async with await get_db_session() as session:
        participant = await session.get(ScenarioParticipant, participant_uid)
        if participant is None:
            raise ParticipantNotFoundError(participant_id)
        play_session = await session.get(ScenarioPlaySession, participant.play_session_id)
        if play_session is None:
            raise RoomNotFoundError(str(participant.play_session_id))

        if participant.user_id == user_uid:
            return {
                "room_id": str(play_session.id),
                "participant_id": str(participant.id),
                "is_owner": True,
                "is_host": False,
                "is_gm": False,
            }
        if allow_gm and play_session.host_user_id == user_uid:
            return {
                "room_id": str(play_session.id),
                "participant_id": str(participant.id),
                "is_owner": False,
                "is_host": True,
                "is_gm": True,
            }
        if allow_gm:
            gm_result = await session.execute(
                select(ScenarioParticipant).where(
                    ScenarioParticipant.play_session_id == play_session.id,
                    ScenarioParticipant.user_id == user_uid,
                    ScenarioParticipant.role == "gm",
                    ScenarioParticipant.is_active_participant.is_(True),
                )
            )
            gm = gm_result.scalars().first()
            if gm:
                return {
                    "room_id": str(play_session.id),
                    "participant_id": str(participant.id),
                    "is_owner": False,
                    "is_host": False,
                    "is_gm": True,
                }
        raise TRPGPlayError("この参加者として操作できません", status_code=403)
