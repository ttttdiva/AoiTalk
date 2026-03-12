"""HeartbeatDefinition のテスト"""
import pytest
from src.heartbeat.models import HeartbeatDefinition


class TestHeartbeatDefinition:
    def test_basic_creation(self):
        hb = HeartbeatDefinition(
            name="test",
            description="テスト",
            checklist="- 項目1\n- 項目2",
        )
        assert hb.name == "test"
        assert hb.description == "テスト"
        assert hb.interval_minutes == 30
        assert hb.enabled is True
        assert hb.notify_channel == "websocket"
        assert hb.active_hours is None

    def test_custom_values(self):
        hb = HeartbeatDefinition(
            name="custom",
            description="カスタム",
            checklist="- チェック",
            interval_minutes=60,
            enabled=False,
            active_hours={"start": "08:00", "end": "20:00", "timezone": "Asia/Tokyo"},
            notify_channel="none",
        )
        assert hb.interval_minutes == 60
        assert hb.enabled is False
        assert hb.active_hours["timezone"] == "Asia/Tokyo"
        assert hb.notify_channel == "none"

    def test_to_dict(self):
        hb = HeartbeatDefinition(
            name="test",
            description="テスト",
            checklist="- 項目",
            interval_minutes=15,
        )
        d = hb.to_dict()
        assert d["name"] == "test"
        assert d["description"] == "テスト"
        assert d["checklist"] == "- 項目"
        assert d["interval_minutes"] == 15
        assert d["enabled"] is True
        assert d["notify_channel"] == "websocket"
        assert "active_hours" not in d

    def test_to_dict_with_active_hours(self):
        hb = HeartbeatDefinition(
            name="test",
            description="テスト",
            checklist="- 項目",
            active_hours={"start": "09:00", "end": "22:00"},
        )
        d = hb.to_dict()
        assert "active_hours" in d
        assert d["active_hours"]["start"] == "09:00"
