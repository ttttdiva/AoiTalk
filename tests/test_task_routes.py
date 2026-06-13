from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.memory.models import TaskRecurrenceRule
from src.api.task_routes import (
    TaskRecurrencePayload,
    UpdateTaskPayload,
    _build_update_task_updates,
    _parse_datetime,
    _parse_wall_clock_datetime,
)


def test_parse_datetime_accepts_utc_z_suffix():
    parsed = _parse_datetime("2026-03-20T00:00:00.000Z", "start_from")

    assert parsed == datetime(2026, 3, 20, 0, 0, 0)


def test_parse_datetime_converts_offset_aware_values_to_naive_utc():
    parsed = _parse_datetime("2026-03-20T09:30:00+09:00", "start_from")

    assert parsed == datetime(2026, 3, 20, 0, 30, 0)


def test_parse_wall_clock_datetime_keeps_offset_aware_clock_time():
    parsed = _parse_wall_clock_datetime(
        "2026-03-20T09:30:00+09:00", "start_at"
    )

    assert parsed == datetime(2026, 3, 20, 9, 30, 0)


def test_parse_wall_clock_datetime_keeps_z_clock_time():
    parsed = _parse_wall_clock_datetime("2026-03-20T09:30:00.000Z", "start_at")

    assert parsed == datetime(2026, 3, 20, 9, 30, 0)


def test_parse_datetime_rejects_invalid_values():
    with pytest.raises(HTTPException) as exc_info:
        _parse_datetime("not-a-date", "start_from")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid start_from"


def test_update_task_payload_uses_patch_semantics_for_omitted_fields():
    updates = _build_update_task_updates(UpdateTaskPayload(status="open"))

    assert updates == {"status": "open"}


def test_update_task_payload_allows_explicit_null_fields():
    updates = _build_update_task_updates(
        UpdateTaskPayload(description=None, start_at=None, end_at=None)
    )

    assert updates == {"description": None, "start_at": None, "end_at": None}


def test_task_recurrence_payload_allows_missing_rrule_for_route_contract():
    payload = TaskRecurrencePayload()

    assert payload.rrule is None


def test_task_recurrence_rule_serializes_frontend_metadata_fields():
    rule_id = uuid4()
    task_id = uuid4()
    rule = TaskRecurrenceRule(
        id=rule_id,
        task_id=task_id,
        rrule="FREQ=DAILY;INTERVAL=1",
        timezone="Asia/Tokyo",
        horizon_days=30,
        trigger_status="closed",
        create_new=False,
        recur_forever=True,
        reset_status_to="open",
        end_count=5,
        end_date=datetime(2026, 6, 10),
        skip_weekend=True,
        skip_holiday=False,
    )

    assert rule.to_dict() == {
        "id": str(rule_id),
        "task_id": str(task_id),
        "rrule": "FREQ=DAILY;INTERVAL=1",
        "timezone": "Asia/Tokyo",
        "horizon_days": 30,
        "trigger_status": "closed",
        "create_new": False,
        "recur_forever": True,
        "reset_status_to": "open",
        "end_count": 5,
        "end_date": "2026-06-10T00:00:00",
        "skip_weekend": True,
        "skip_holiday": False,
        "created_at": None,
        "updated_at": None,
    }
