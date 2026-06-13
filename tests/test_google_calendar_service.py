from src.services.google_calendar_service import GoogleCalendarService


def test_auto_sync_requires_timed_start() -> None:
    service = GoogleCalendarService()

    assert service._task_has_timed_start(
        {
            "start_at": "2026-04-28T09:30:00",
            "all_day": False,
        }
    )


def test_auto_sync_skips_all_day_or_date_only_start() -> None:
    service = GoogleCalendarService()

    assert not service._task_has_timed_start(
        {
            "start_at": "2026-04-28T09:30:00",
            "all_day": True,
        }
    )
    assert not service._task_has_timed_start(
        {
            "start_at": "2026-04-28T00:00:00",
            "all_day": False,
        }
    )
    assert not service._task_has_timed_start(
        {
            "start_at": "2026-04-28T00:00:00Z",
            "all_day": False,
        }
    )
    assert not service._task_has_timed_start(
        {
            "start_at": None,
            "all_day": False,
        }
    )


def test_google_calendar_metadata_must_be_object() -> None:
    service = GoogleCalendarService()

    assert service._get_google_calendar_metadata({"google_calendar": "bad"}) == {}
    assert service._get_google_calendar_metadata(
        {"google_calendar": {"auto_event_id": "event-1"}}
    ) == {"auto_event_id": "event-1"}


def test_event_payload_is_notification_only() -> None:
    service = GoogleCalendarService()

    payload = service._build_event_payload(
        {
            "id": "task-1",
            "title": "Notify me",
            "description": "Do not sync this",
            "project_name": "Do not sync this either",
            "start_at": "2026-04-28T09:30:00",
            "end_at": "2026-04-28T10:00:00",
            "all_day": False,
            "reminder_offsets": [5, "15", "bad", 5],
        },
        default_event_reminder_minutes=30,
    )

    assert payload["summary"] == "Notify me"
    assert "description" not in payload
    assert payload["start"] == {
        "dateTime": "2026-04-28T09:30:00",
        "timeZone": "Asia/Tokyo",
    }
    assert payload["end"] == {
        "dateTime": "2026-04-28T10:00:00",
        "timeZone": "Asia/Tokyo",
    }
    assert payload["reminders"] == {
        "useDefault": False,
        "overrides": [
            {"method": "popup", "minutes": 5},
            {"method": "popup", "minutes": 15},
        ],
    }


def test_event_payload_uses_default_reminder_when_task_has_none() -> None:
    service = GoogleCalendarService()

    payload = service._build_event_payload(
        {
            "id": "task-1",
            "title": "Notify me",
            "start_at": "2026-04-28T09:30:00",
            "all_day": False,
            "reminder_offsets": [],
        },
        default_event_reminder_minutes=20,
    )

    assert payload["reminders"]["overrides"] == [
        {"method": "popup", "minutes": 20}
    ]


def test_event_payload_preserves_wall_clock_for_offset_inputs() -> None:
    service = GoogleCalendarService()

    payload = service._build_event_payload(
        {
            "id": "task-1",
            "title": "Notify me",
            "start_at": "2026-04-28T09:30:00+09:00",
            "end_at": "2026-04-28T10:00:00Z",
            "all_day": False,
            "reminder_offsets": [],
            "timezone": "Asia/Tokyo",
        },
        default_event_reminder_minutes=0,
    )

    assert payload["start"] == {
        "dateTime": "2026-04-28T09:30:00",
        "timeZone": "Asia/Tokyo",
    }
    assert payload["end"] == {
        "dateTime": "2026-04-28T10:00:00",
        "timeZone": "Asia/Tokyo",
    }


def test_event_payload_normalizes_legacy_utc_task_timezone() -> None:
    service = GoogleCalendarService()

    payload = service._build_event_payload(
        {
            "id": "task-1",
            "title": "Daily standup",
            "start_at": "2026-05-08T10:00:00",
            "end_at": "2026-05-08T10:30:00",
            "all_day": False,
            "reminder_offsets": [],
            "recurrence_timezone": "UTC",
        },
        default_event_reminder_minutes=0,
    )

    assert payload["start"] == {
        "dateTime": "2026-05-08T10:00:00",
        "timeZone": "Asia/Tokyo",
    }
    assert payload["end"] == {
        "dateTime": "2026-05-08T10:30:00",
        "timeZone": "Asia/Tokyo",
    }
