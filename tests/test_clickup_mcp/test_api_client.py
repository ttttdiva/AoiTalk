"""api_client.py のテスト: 日時変換、クライアント初期化。"""

import os
from datetime import datetime
from unittest.mock import patch

import pytest

from src.tools.external.clickup_mcp.api_client import (
    ClickUpAPIClient,
    parse_datetime_to_ms,
)


# --- parse_datetime_to_ms ---


class TestParseDatetimeToMs:
    """日時変換ユーティリティのテスト。"""

    def test_date_only_yyyy_mm_dd(self):
        ts, has_time = parse_datetime_to_ms("2026-02-19")
        assert ts is not None
        assert has_time is False
        dt = datetime.fromtimestamp(ts / 1000)
        assert dt.year == 2026
        assert dt.month == 2
        assert dt.day == 19

    def test_date_with_time(self):
        ts, has_time = parse_datetime_to_ms("2026-02-19", "14:30")
        assert ts is not None
        assert has_time is True
        dt = datetime.fromtimestamp(ts / 1000)
        assert dt.hour == 14
        assert dt.minute == 30

    def test_iso_datetime_t_separator(self):
        ts, has_time = parse_datetime_to_ms("2026-02-19T10:00")
        assert ts is not None
        assert has_time is True

    def test_iso_datetime_with_seconds(self):
        ts, has_time = parse_datetime_to_ms("2026-02-19T10:00:30")
        assert ts is not None
        assert has_time is True

    def test_slash_date_format(self):
        ts, has_time = parse_datetime_to_ms("2026/02/19")
        assert ts is not None
        assert has_time is False

    def test_slash_datetime_format(self):
        ts, has_time = parse_datetime_to_ms("2026/02/19 14:30")
        assert ts is not None
        assert has_time is True

    def test_space_datetime_format(self):
        ts, has_time = parse_datetime_to_ms("2026-02-19 14:30")
        assert ts is not None
        assert has_time is True

    def test_empty_string(self):
        ts, has_time = parse_datetime_to_ms("")
        assert ts is None
        assert has_time is False

    def test_none_input(self):
        ts, has_time = parse_datetime_to_ms(None)
        assert ts is None
        assert has_time is False

    def test_invalid_format(self):
        ts, has_time = parse_datetime_to_ms("not-a-date")
        assert ts is None
        assert has_time is False

    def test_whitespace_handling(self):
        ts, has_time = parse_datetime_to_ms("  2026-02-19  ", "  14:30  ")
        assert ts is not None
        assert has_time is True

    def test_date_with_time_hms(self):
        ts, has_time = parse_datetime_to_ms("2026-02-19", "14:30:00")
        assert ts is not None
        assert has_time is True

    def test_returns_milliseconds(self):
        """タイムスタンプがミリ秒単位であること。"""
        ts, _ = parse_datetime_to_ms("2026-02-19")
        assert ts is not None
        # ミリ秒は13桁前後
        assert ts > 1_000_000_000_000


# --- ClickUpAPIClient 初期化 ---


class TestClickUpAPIClient:
    """APIクライアントの初期化テスト。"""

    @patch.dict(os.environ, {
        "CLICKUP_API_KEY": "pk_test123",
        "CLICKUP_TEAM_ID": "team_456",
        "CLICKUP_DEFAULT_LIST_ID": "list_789",
    })
    def test_env_vars_loaded(self):
        client = ClickUpAPIClient()
        assert client.api_key == "pk_test123"
        assert client.team_id == "team_456"
        assert client.default_list_id == "list_789"

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_env_vars_uses_defaults(self):
        client = ClickUpAPIClient()
        assert client.api_key == ""
        assert client.team_id == ""
        assert client.default_list_id == ""

    @patch.dict(os.environ, {"CLICKUP_API_KEY": "pk_test"})
    def test_headers_use_raw_api_key(self):
        """Authorization ヘッダーは Bearer なしで API キーを直接使う。"""
        client = ClickUpAPIClient()
        headers = client.headers
        assert headers["Authorization"] == "pk_test"
        assert "Bearer" not in headers["Authorization"]

    @patch.dict(os.environ, {"CLICKUP_API_KEY": "pk_test"})
    def test_headers_include_content_type(self):
        client = ClickUpAPIClient()
        assert client.headers["Content-Type"] == "application/json"
