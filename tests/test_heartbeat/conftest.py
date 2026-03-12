"""Heartbeat テスト用 conftest"""
import pytest
from pathlib import Path


@pytest.fixture
def sample_heartbeats_dir(tmp_path):
    """一時Heartbeatディレクトリを作成"""
    hb_dir = tmp_path / "heartbeats"
    hb_dir.mkdir()
    return hb_dir


@pytest.fixture
def sample_heartbeat_yaml(sample_heartbeats_dir):
    """サンプルHeartbeat YAMLファイルを作成"""
    yaml_content = """
name: test_check
description: テスト用チェック
checklist: |
  - テスト項目1を確認
  - テスト項目2を確認
interval_minutes: 15
enabled: true
active_hours:
  start: "09:00"
  end: "22:00"
  timezone: Asia/Tokyo
notify_channel: websocket
"""
    path = sample_heartbeats_dir / "test_check.yaml"
    path.write_text(yaml_content, encoding="utf-8")
    return path
