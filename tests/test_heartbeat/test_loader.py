"""HeartbeatLoader のテスト"""
import pytest
from pathlib import Path
from src.heartbeat.loader import load_heartbeat_from_yaml, load_all_heartbeats, save_heartbeat_to_yaml, delete_heartbeat_yaml
from src.heartbeat.models import HeartbeatDefinition
from src.heartbeat.registry import HeartbeatRegistry


class TestLoadHeartbeatFromYaml:
    def test_load_valid_yaml(self, sample_heartbeat_yaml):
        hb = load_heartbeat_from_yaml(sample_heartbeat_yaml)
        assert hb is not None
        assert hb.name == "test_check"
        assert hb.description == "テスト用チェック"
        assert hb.interval_minutes == 15
        assert hb.enabled is True
        assert hb.active_hours is not None
        assert hb.active_hours["timezone"] == "Asia/Tokyo"

    def test_load_nonexistent_file(self, tmp_path):
        hb = load_heartbeat_from_yaml(tmp_path / "nonexistent.yaml")
        assert hb is None

    def test_load_invalid_yaml(self, tmp_path):
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text(": invalid: yaml: [", encoding="utf-8")
        hb = load_heartbeat_from_yaml(bad_file)
        assert hb is None


class TestLoadAllHeartbeats:
    def test_load_from_directory(self, sample_heartbeats_dir, sample_heartbeat_yaml):
        heartbeats = load_all_heartbeats(sample_heartbeats_dir)
        assert len(heartbeats) >= 1
        assert any(h.name == "test_check" for h in heartbeats)

    def test_load_from_empty_directory(self, tmp_path):
        empty_dir = tmp_path / "empty_heartbeats"
        empty_dir.mkdir()
        heartbeats = load_all_heartbeats(empty_dir)
        assert heartbeats == []

    def test_creates_directory_if_missing(self, tmp_path):
        new_dir = tmp_path / "new_heartbeats_dir"
        assert not new_dir.exists()
        heartbeats = load_all_heartbeats(new_dir)
        assert new_dir.exists()
        assert heartbeats == []


class TestSaveHeartbeatToYaml:
    def test_save_and_reload(self, sample_heartbeats_dir):
        hb = HeartbeatDefinition(
            name="saved_check",
            description="保存テスト",
            checklist="- テスト項目",
            interval_minutes=60,
        )
        result = save_heartbeat_to_yaml(hb, sample_heartbeats_dir)
        assert result is True

        saved_path = sample_heartbeats_dir / "saved_check.yaml"
        assert saved_path.exists()

        loaded = load_heartbeat_from_yaml(saved_path)
        assert loaded.name == "saved_check"
        assert loaded.description == "保存テスト"
        assert loaded.interval_minutes == 60

    def test_save_with_active_hours(self, sample_heartbeats_dir):
        hb = HeartbeatDefinition(
            name="hours_check",
            description="時間制限テスト",
            checklist="- 項目",
            active_hours={"start": "08:00", "end": "20:00", "timezone": "UTC"},
        )
        result = save_heartbeat_to_yaml(hb, sample_heartbeats_dir)
        assert result is True

        loaded = load_heartbeat_from_yaml(sample_heartbeats_dir / "hours_check.yaml")
        assert loaded.active_hours["timezone"] == "UTC"


class TestDeleteHeartbeatYaml:
    def test_delete_existing(self, sample_heartbeats_dir, sample_heartbeat_yaml):
        assert sample_heartbeat_yaml.exists()
        result = delete_heartbeat_yaml("test_check", sample_heartbeats_dir)
        assert result is True
        assert not sample_heartbeat_yaml.exists()

    def test_delete_nonexistent(self, sample_heartbeats_dir):
        result = delete_heartbeat_yaml("nonexistent", sample_heartbeats_dir)
        assert result is False
