from datetime import datetime
from uuid import UUID

import pytest

from src.api.sync_routes import (
    _conversation_user_ids,
    _ensure_not_stale,
    _task_updates_from_payload,
    _time_entry_values_from_payload,
)
from src.memory.models import Tag
from src.services.task_management_service import TaskManagementError


def test_ensure_not_stale_allows_matching_base_timestamp():
    _ensure_not_stale(datetime(2026, 4, 20, 12, 0, 0), "2026-04-20T12:00:00")


def test_ensure_not_stale_raises_conflict_for_newer_server_value():
    with pytest.raises(TaskManagementError) as exc_info:
        _ensure_not_stale(datetime(2026, 4, 20, 12, 0, 1), "2026-04-20T12:00:00")

    assert exc_info.value.status_code == 409
    assert "updated on the server" in exc_info.value.message


def test_time_entry_values_from_payload_normalizes_optional_fields():
    payload = _time_entry_values_from_payload(
        {
            "task_id": "8f8b3df1-cf57-40c7-b74d-0b6eddbf7e3f",
            "occurrence_id": "",
            "started_at": "2026-04-20T09:00:00+09:00",
            "ended_at": "2026-04-20T10:30:00+09:00",
            "note": "focus block",
            "metadata": {"offline": True},
        }
    )

    assert str(payload["task_id"]) == "8f8b3df1-cf57-40c7-b74d-0b6eddbf7e3f"
    assert payload["occurrence_id"] is None
    assert payload["started_at"] == datetime(2026, 4, 20, 0, 0, 0)
    assert payload["ended_at"] == datetime(2026, 4, 20, 1, 30, 0)
    assert payload["entry_metadata"] == {"offline": True}


def test_task_updates_from_payload_preserves_wall_clock_task_times():
    payload = _task_updates_from_payload(
        {
            "start_at": "2026-04-20T09:00:00+09:00",
            "end_at": "2026-04-20T10:30:00.000Z",
        }
    )

    assert payload["start_at"] == datetime(2026, 4, 20, 9, 0, 0)
    assert payload["end_at"] == datetime(2026, 4, 20, 10, 30, 0)


def test_conversation_user_ids_include_legacy_default_user():
    user_id = UUID("00000000-0000-0000-0000-000000000123")

    assert _conversation_user_ids(user_id) == [
        "00000000-0000-0000-0000-000000000123",
        "default_user",
    ]


def test_tag_model_matches_space_scoped_schema():
    assert "space_id" in Tag.__table__.c
    assert "project_id" not in Tag.__table__.c
