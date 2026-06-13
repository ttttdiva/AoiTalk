from datetime import datetime
import uuid

from src.models.ecc_models import ScenarioParticipant, ScenarioPlayLog
from src.services.trpg_ai_npc_service import (
    _active_ai_npcs,
    _fallback_public_npc_content,
    _logs_after_last_seen,
    _format_public_npc_content,
    _looks_like_public_reply_fallback,
    _merge_private_state,
    _observed_requires_public_reply,
    _observed_logs_for_npc_turn,
    _parse_uuid,
    _safe_json_object,
    _strip_public_quote_wrapping,
    parse_npc_strategy_marker,
    schedule_ai_npc_strategy_shared_state,
)
from src.models.ecc_models import ScenarioCharacter


def test_participant_to_dict_does_not_expose_private_state():
    participant = ScenarioParticipant(
        id=uuid.uuid4(),
        play_session_id=uuid.uuid4(),
        display_name="秘匿NPC",
        role="npc",
        participant_kind="ai_character",
        private_state={"last_thought": "これは公開してはいけない"},
        last_observed_log_id=uuid.uuid4(),
    )

    data = participant.to_dict()

    assert "private_state" not in data
    assert "last_observed_log_id" not in data


def test_merge_private_state_keeps_hidden_memory_only():
    result = {
        "internal_thought": "PL Aを疑っている。",
        "memory_update": {
            "notes": ["Aは鍵を隠した可能性がある"],
            "goals": ["鍵の所在を探る"],
            "secrets": ["本当の目的は儀式の阻止"],
            "relationships": {"A": "警戒対象"},
        },
        "action_type": "none",
        "public_content": "",
    }
    log = ScenarioPlayLog(
        id=uuid.uuid4(),
        play_session_id=uuid.uuid4(),
        log_type="narration",
        content="部屋に鍵の音が響いた。",
        created_at=datetime(2026, 5, 9, 12, 0, 0),
    )

    state = _merge_private_state({}, result, [log])

    assert state["last_thought"] == "PL Aを疑っている。"
    assert state["notes"] == ["Aは鍵を隠した可能性がある"]
    assert state["goals"] == ["鍵の所在を探る"]
    assert state["secrets"] == ["本当の目的は儀式の阻止"]
    assert state["relationships"] == {"A": "警戒対象"}
    assert state["last_observed_at"] == "2026-05-09T12:00:00"


def test_merge_private_state_does_not_mutate_current_state_in_place():
    current = {"notes": ["既存メモ"]}
    result = {
        "internal_thought": "新しい思考",
        "memory_update": {"notes": ["新しいメモ"]},
    }

    state = _merge_private_state(current, result, [])

    assert current == {"notes": ["既存メモ"]}
    assert state["notes"] == ["既存メモ", "新しいメモ"]
    assert state["last_thought"] == "新しい思考"


def test_format_public_npc_content_removes_duplicate_quote_wrapping():
    assert _strip_public_quote_wrapping("「「監査しましょう」」") == "監査しましょう"
    assert _strip_public_quote_wrapping("「「監査しましょう?”」") == "監査しましょう?"

    log_type, content = _format_public_npc_content(
        "御影 真司",
        "public_speech",
        "「残りカードを公開しましょう」",
    )

    assert log_type == "speech"
    assert content == "御影 真司: 「残りカードを公開しましょう」"


def test_safe_json_object_accepts_fenced_json():
    parsed = _safe_json_object(
        """```json
{"action_type": "none", "public_content": ""}
```"""
    )

    assert parsed == {"action_type": "none", "public_content": ""}


def test_logs_after_last_seen_returns_only_newer_logs():
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    logs = [
        ScenarioPlayLog(
            id=first_id,
            play_session_id=uuid.uuid4(),
            log_type="speech",
            content="先のログ",
            created_at=datetime(2026, 5, 9, 12, 0, 0),
        ),
        ScenarioPlayLog(
            id=second_id,
            play_session_id=uuid.uuid4(),
            log_type="speech",
            content="新しいログ",
            created_at=datetime(2026, 5, 9, 12, 1, 0),
        ),
    ]

    observed = _logs_after_last_seen(logs, first_id)

    assert [log.id for log in observed] == [second_id]


def test_observed_logs_for_npc_turn_includes_previous_npc_speech():
    room_id = uuid.uuid4()
    first_npc_id = uuid.uuid4()
    second_npc = ScenarioParticipant(
        id=uuid.uuid4(),
        play_session_id=room_id,
        display_name="後続NPC",
        role="npc",
        participant_kind="ai_character",
        last_observed_log_id=None,
    )
    gm_log = ScenarioPlayLog(
        id=uuid.uuid4(),
        play_session_id=room_id,
        participant_id=None,
        log_type="narration",
        content="GM: 作戦タイムです。",
        created_at=datetime(2026, 5, 9, 12, 0, 0),
    )
    first_npc_log = ScenarioPlayLog(
        id=uuid.uuid4(),
        play_session_id=room_id,
        participant_id=first_npc_id,
        log_type="speech",
        content="先行NPC: 「監査役を決めましょう」",
        created_at=datetime(2026, 5, 9, 12, 1, 0),
    )

    observed = _observed_logs_for_npc_turn([gm_log, first_npc_log], second_npc)

    assert [log.id for log in observed] == [gm_log.id, first_npc_log.id]


def test_parse_npc_strategy_marker_defaults_to_30_seconds():
    parsed = parse_npc_strategy_marker("phase=投票前相談,focus=監査の穴")

    assert parsed == {
        "phase": "投票前相談",
        "focus": "監査の穴",
        "delay_seconds": 30,
    }


def test_parse_uuid_accepts_valid_string_and_rejects_invalid_value():
    uid = uuid.uuid4()

    assert _parse_uuid(str(uid)) == uid
    assert _parse_uuid("not-a-uuid") is None


def test_schedule_ai_npc_strategy_shared_state_sets_due_state():
    now = datetime(2026, 5, 9, 12, 0, 0)

    shared, strategy = schedule_ai_npc_strategy_shared_state(
        {"round": 2},
        "phase=作戦タイム,delay=45,focus=六人同盟",
        trigger="test",
        now=now,
    )

    assert shared["round"] == 2
    assert strategy["status"] == "scheduled"
    assert strategy["phase"] == "作戦タイム"
    assert strategy["focus"] == "六人同盟"
    assert strategy["delay_seconds"] == 45
    assert strategy["scheduled_at"] == "2026-05-09T12:00:00Z"
    assert strategy["due_at"] == "2026-05-09T12:00:45Z"
    assert shared["ai_npc_strategy"] == strategy


def test_active_ai_npcs_prioritizes_strategy_profiles():
    room_id = uuid.uuid4()
    high = ScenarioParticipant(
        id=uuid.uuid4(),
        play_session_id=room_id,
        display_name="主導NPC",
        role="npc",
        participant_kind="ai_character",
        seat_index=5,
        is_active_participant=True,
    )
    low = ScenarioParticipant(
        id=uuid.uuid4(),
        play_session_id=room_id,
        display_name="通常NPC",
        role="npc",
        participant_kind="ai_character",
        seat_index=1,
        is_active_participant=True,
    )
    characters = [
        ScenarioCharacter(
            name="主導NPC",
            relationships=[
                {
                    "type": "ai_npc_strategy_profile",
                    "priority": 1,
                }
            ],
        ),
        ScenarioCharacter(name="通常NPC", relationships=[]),
    ]

    ordered = _active_ai_npcs([low, high], characters)

    assert [npc.display_name for npc in ordered[:2]] == ["主導NPC", "通常NPC"]


def test_npc_fallback_reply_references_latest_speaker_generically():
    room_id = uuid.uuid4()
    player_id = uuid.uuid4()
    npc = ScenarioParticipant(
        id=uuid.uuid4(),
        play_session_id=room_id,
        display_name="汎用NPC",
        role="npc",
        participant_kind="ai_character",
    )
    player = ScenarioParticipant(
        id=player_id,
        play_session_id=room_id,
        display_name="検証PL",
        role="player",
        participant_kind="human",
    )
    latest = ScenarioPlayLog(
        id=uuid.uuid4(),
        play_session_id=room_id,
        participant_id=player_id,
        log_type="speech",
        content="検証PL: 「監査役を決めませんか」",
        created_at=datetime(2026, 5, 9, 12, 0, 0),
    )

    assert _observed_requires_public_reply([latest], npc, trigger="player_action")
    content = _fallback_public_npc_content(npc, [latest], [player, npc])

    assert "検証PLさんの監査案に賛成" in content
    assert "誰が、いつ、何を確認するか" in content


def test_npc_fallback_reply_uses_scenario_role_voice():
    room_id = uuid.uuid4()
    player_id = uuid.uuid4()
    npc = ScenarioParticipant(
        id=uuid.uuid4(),
        play_session_id=room_id,
        display_name="乾 玲司",
        role="npc",
        participant_kind="ai_character",
    )
    player = ScenarioParticipant(
        id=player_id,
        play_session_id=room_id,
        display_name="検証PL",
        role="player",
        participant_kind="human",
    )
    latest = ScenarioPlayLog(
        id=uuid.uuid4(),
        play_session_id=room_id,
        participant_id=player_id,
        log_type="speech",
        content="検証PL: 「全員5票で固定しませんか」",
        created_at=datetime(2026, 5, 9, 12, 0, 0),
    )

    content = _fallback_public_npc_content(npc, [latest], [player, npc])

    assert "協力案自体は悪くありません" in content
    assert "動ける余地" in content


def test_weak_public_reply_fallback_detection_catches_quote_echo():
    assert _looks_like_public_reply_fallback(
        "白石 円さんの発言を受けます。『白石 円: 「監査しましょう」』の点について、私は条件を確認してから動きたいです。"
    )
    assert not _looks_like_public_reply_fallback("監査役を交代制にするなら、次は誰が確認するか決めましょう。")
