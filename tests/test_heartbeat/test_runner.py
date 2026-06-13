"""HeartbeatRunner のテスト"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.heartbeat.models import HeartbeatDefinition
from src.heartbeat.registry import HeartbeatRegistry
from src.heartbeat.runner import HeartbeatRunner, HEARTBEAT_OK


class TestHeartbeatOkDetection:
    def test_exact_ok(self):
        runner = HeartbeatRunner()
        assert runner._is_heartbeat_ok("HEARTBEAT_OK") is True

    def test_ok_with_whitespace(self):
        runner = HeartbeatRunner()
        assert runner._is_heartbeat_ok("  HEARTBEAT_OK  ") is True

    def test_ok_at_start(self):
        runner = HeartbeatRunner()
        assert runner._is_heartbeat_ok("HEARTBEAT_OK すべて正常です") is True

    def test_ok_at_end(self):
        runner = HeartbeatRunner()
        assert runner._is_heartbeat_ok("すべて正常です HEARTBEAT_OK") is True

    def test_alert_response(self):
        runner = HeartbeatRunner()
        assert runner._is_heartbeat_ok("ディスク使用量が90%に達しています") is False

    def test_empty_response(self):
        runner = HeartbeatRunner()
        assert runner._is_heartbeat_ok("") is False


class TestActiveHours:
    def test_no_active_hours(self):
        runner = HeartbeatRunner()
        hb = HeartbeatDefinition(
            name="test", description="", checklist="", active_hours=None
        )
        assert runner._is_in_active_hours(hb) is True

    def test_empty_active_hours(self):
        runner = HeartbeatRunner()
        hb = HeartbeatDefinition(
            name="test", description="", checklist="", active_hours={}
        )
        assert runner._is_in_active_hours(hb) is True


class TestGetStatus:
    def test_initial_status(self):
        runner = HeartbeatRunner()
        status = runner.get_status()
        assert status["running"] is False
        assert status["llm_client_set"] is False
        assert status["last_results"] == {}


@pytest.mark.asyncio
class TestStartupBehavior:
    async def test_start_defers_initial_enabled_heartbeat(self):
        runner = HeartbeatRunner()

        from src.heartbeat.registry import get_heartbeat_registry

        registry = get_heartbeat_registry()
        hb = HeartbeatDefinition(
            name="startup_defer_test",
            description="テスト",
            checklist="- 起動直後に実行しない",
            interval_minutes=120,
        )
        registry.register(hb)

        try:
            before = datetime_now_timestamp()
            await runner.start()
            after = datetime_now_timestamp()
            assert before <= runner._last_run["startup_defer_test"] <= after
        finally:
            await runner.stop()
            registry.unregister("startup_defer_test")

    async def test_trigger_returns_timeout_result(self):
        runner = HeartbeatRunner()
        runner._heartbeat_timeout_seconds = 0.01
        runner._get_project_contexts = AsyncMock(return_value=[])

        mock_llm = AsyncMock()

        async def slow_response(_prompt):
            await asyncio.sleep(60)
            return HEARTBEAT_OK

        mock_llm.generate_response_async = slow_response
        runner.set_llm_client(mock_llm)

        from src.heartbeat.registry import get_heartbeat_registry

        registry = get_heartbeat_registry()
        hb = HeartbeatDefinition(
            name="timeout_test",
            description="テスト",
            checklist="- timeout",
        )
        registry.register(hb)

        try:
            result = await runner.trigger("timeout_test")
            assert result["status"] == "timeout"
            assert "timed out" in result["response"]
        finally:
            registry.unregister("timeout_test")


def datetime_now_timestamp():
    import datetime

    return datetime.datetime.now().timestamp()


@pytest.mark.asyncio
class TestTrigger:
    async def test_trigger_nonexistent(self):
        runner = HeartbeatRunner()
        result = await runner.trigger("nonexistent")
        assert result is None

    async def test_trigger_without_llm(self):
        runner = HeartbeatRunner()
        # レジストリにHeartbeatを直接登録
        from src.heartbeat.registry import get_heartbeat_registry
        registry = get_heartbeat_registry()
        hb = HeartbeatDefinition(
            name="trigger_test", description="テスト", checklist="- 項目"
        )
        registry.register(hb)

        try:
            result = await runner.trigger("trigger_test")
            assert result is not None
            assert result["status"] == "no_llm_client"
        finally:
            registry.unregister("trigger_test")

    async def test_trigger_with_mock_llm_ok(self):
        runner = HeartbeatRunner()
        mock_llm = AsyncMock()
        mock_llm.generate_response_async = AsyncMock(return_value="HEARTBEAT_OK")
        runner.set_llm_client(mock_llm)

        from src.heartbeat.registry import get_heartbeat_registry
        registry = get_heartbeat_registry()
        hb = HeartbeatDefinition(
            name="mock_ok_test", description="テスト", checklist="- 全部OK"
        )
        registry.register(hb)

        try:
            result = await runner.trigger("mock_ok_test")
            assert result["status"] == "ok"
            assert result["is_alert"] is False
        finally:
            registry.unregister("mock_ok_test")

    async def test_trigger_with_mock_llm_alert(self):
        runner = HeartbeatRunner()
        mock_llm = AsyncMock()
        mock_llm.generate_response_async = AsyncMock(return_value="エラーが検出されました")
        runner.set_llm_client(mock_llm)

        mock_broadcast = AsyncMock()
        runner.set_broadcast_fn(mock_broadcast)

        from src.heartbeat.registry import get_heartbeat_registry
        registry = get_heartbeat_registry()
        hb = HeartbeatDefinition(
            name="mock_alert_test", description="テスト", checklist="- エラーチェック"
        )
        registry.register(hb)

        try:
            result = await runner.trigger("mock_alert_test")
            assert result["status"] == "alert"
            assert result["is_alert"] is True
            # WebSocket通知が呼ばれたことを確認
            mock_broadcast.assert_called_once()
            call_args = mock_broadcast.call_args[0][0]
            assert call_args["type"] == "heartbeat_alert"
            assert call_args["data"]["heartbeat_name"] == "mock_alert_test"
        finally:
            registry.unregister("mock_alert_test")
