from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.services.trpg_play_service import (
    GM_TARGET_ID,
    _can_delete_room,
    _normalize_room_status_filter,
    _normalize_target_ids,
    _viewer_can_see_disclosure,
    _viewer_can_see_private_message,
    require_room_participation_access,
)

import src.services.trpg_play_service as trpg_play_service


def _participant(role="player", participant_id=None):
    return SimpleNamespace(id=participant_id or uuid4(), role=role)


def test_public_disclosure_is_visible_to_any_participant():
    disclosure = SimpleNamespace(
        visibility="public",
        creator_participant_id=None,
        target_participant_ids=[],
    )

    assert _viewer_can_see_disclosure(disclosure, _participant())


def test_private_disclosure_is_limited_to_target_creator_and_host():
    target = _participant()
    other = _participant()
    disclosure = SimpleNamespace(
        visibility="private",
        creator_participant_id=uuid4(),
        target_participant_ids=[str(target.id)],
    )

    assert _viewer_can_see_disclosure(disclosure, target)
    assert not _viewer_can_see_disclosure(disclosure, other)
    assert _viewer_can_see_disclosure(disclosure, other, is_host=True)


def test_gm_target_private_message_is_visible_to_gm_only():
    gm = _participant(role="gm")
    player = _participant(role="player")
    message = SimpleNamespace(
        sender_participant_id=uuid4(),
        target_participant_ids=[GM_TARGET_ID],
    )

    assert _viewer_can_see_private_message(message, gm)
    assert not _viewer_can_see_private_message(message, player)


def test_normalize_target_ids_deduplicates_uuid_and_gm_targets():
    uid = str(uuid4())

    assert _normalize_target_ids([uid, uid.upper(), "gm", "GM", "invalid"]) == [
        uid,
        GM_TARGET_ID,
    ]


def test_normalize_room_status_filter_accepts_all_sentinel_values():
    assert _normalize_room_status_filter("all") is None
    assert _normalize_room_status_filter(" ALL ") is None
    assert _normalize_room_status_filter("") is None
    assert _normalize_room_status_filter(None) is None
    assert _normalize_room_status_filter("completed") == "completed"


def test_room_delete_permission_allows_host_or_admin():
    host_id = uuid4()
    other_id = uuid4()
    play_session = SimpleNamespace(host_user_id=host_id)

    assert _can_delete_room(play_session, host_id, is_admin=False)
    assert _can_delete_room(play_session, other_id, is_admin=True)
    assert not _can_delete_room(play_session, other_id, is_admin=False)


@pytest.mark.asyncio
async def test_public_room_view_access_allows_join_interaction(monkeypatch):
    async def fake_view_access(room_id, user_id, invite_code=None):
        return {
            "room_id": room_id,
            "is_public": True,
            "is_host": False,
            "is_participant": False,
            "invited": False,
        }

    monkeypatch.setattr(trpg_play_service, "require_room_view_access", fake_view_access)

    access = await require_room_participation_access("room-1", str(uuid4()))

    assert access["is_public"] is True
