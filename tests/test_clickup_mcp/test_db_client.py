"""db_client.py のテスト: 初期化と graceful degradation。"""

import os
import sys
import types
from unittest.mock import patch, MagicMock

import pytest

# psycopg2 未インストール環境でもテスト可能にするためスタブ登録
if "psycopg2" not in sys.modules:
    _psycopg2_stub = types.ModuleType("psycopg2")
    _psycopg2_stub.connect = MagicMock()
    sys.modules["psycopg2"] = _psycopg2_stub

from src.tools.external.clickup_mcp.db_client import ClickUpDBClient


class TestClickUpDBClient:
    """DBクライアントの初期化テスト。"""

    @patch.dict(os.environ, {}, clear=True)
    def test_disabled_when_no_password(self):
        """POSTGRES_PASSWORD が未設定のとき disabled になること。"""
        client = ClickUpDBClient()
        assert client.enabled is False

    @patch.dict(os.environ, {"POSTGRES_PASSWORD": "testpass"})
    @patch("src.tools.external.clickup_mcp.db_client.ClickUpDBClient._setup")
    def test_init_calls_setup(self, mock_setup):
        """__init__ で _setup が呼ばれること。"""
        client = ClickUpDBClient()
        mock_setup.assert_called_once()

    @patch.dict(os.environ, {
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "test_db",
        "POSTGRES_USER": "test_user",
        "POSTGRES_PASSWORD": "test_pass",
    })
    def test_graceful_degradation_on_connection_error(self):
        """接続失敗時に例外を投げずに disabled になること。"""
        with patch("psycopg2.connect", side_effect=Exception("Connection refused")):
            client = ClickUpDBClient()
            assert client.enabled is False

    def test_get_task_summary_returns_none_when_disabled(self):
        """disabled 時に get_task_summary が None を返すこと。"""
        with patch.dict(os.environ, {}, clear=True):
            client = ClickUpDBClient()
            assert client.get_task_summary() is None

    def test_get_weekly_tasks_returns_none_when_disabled(self):
        """disabled 時に get_weekly_tasks が None を返すこと。"""
        with patch.dict(os.environ, {}, clear=True):
            client = ClickUpDBClient()
            assert client.get_weekly_tasks() is None

    def test_search_tasks_by_name_returns_none_when_disabled(self):
        """disabled 時に search_tasks_by_name が None を返すこと。"""
        with patch.dict(os.environ, {}, clear=True):
            client = ClickUpDBClient()
            assert client.search_tasks_by_name("test") is None

    @patch.dict(os.environ, {
        "POSTGRES_HOST": "testhost",
        "POSTGRES_PORT": "5433",
        "POSTGRES_DB": "testdb",
        "POSTGRES_USER": "testuser",
        "POSTGRES_PASSWORD": "testpass",
    })
    def test_successful_connection(self):
        """接続成功時に enabled が True になること。"""
        mock_conn = MagicMock()
        with patch("psycopg2.connect", return_value=mock_conn):
            client = ClickUpDBClient()
            assert client.enabled is True
            mock_conn.close.assert_called_once()
