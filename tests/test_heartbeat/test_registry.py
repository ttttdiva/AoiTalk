"""HeartbeatRegistry のテスト"""
import pytest
from src.heartbeat.models import HeartbeatDefinition
from src.heartbeat.registry import HeartbeatRegistry


@pytest.fixture
def registry():
    return HeartbeatRegistry()


@pytest.fixture
def sample_heartbeat():
    return HeartbeatDefinition(
        name="server_check",
        description="サーバーチェック",
        checklist="- API正常確認\n- ディスク確認",
        interval_minutes=30,
    )


class TestHeartbeatRegistry:
    def test_register_and_get(self, registry, sample_heartbeat):
        registry.register(sample_heartbeat)
        assert registry.get("server_check") is sample_heartbeat
        assert len(registry) == 1

    def test_get_not_found(self, registry):
        assert registry.get("nonexistent") is None

    def test_unregister(self, registry, sample_heartbeat):
        registry.register(sample_heartbeat)
        assert registry.unregister("server_check") is True
        assert registry.get("server_check") is None
        assert len(registry) == 0

    def test_unregister_not_found(self, registry):
        assert registry.unregister("nonexistent") is False

    def test_get_all(self, registry, sample_heartbeat):
        registry.register(sample_heartbeat)
        other = HeartbeatDefinition(name="other", description="他", checklist="- x")
        registry.register(other)
        assert len(registry.get_all()) == 2

    def test_get_enabled(self, registry):
        enabled = HeartbeatDefinition(
            name="on", description="有効", checklist="- x", enabled=True
        )
        disabled = HeartbeatDefinition(
            name="off", description="無効", checklist="- x", enabled=False
        )
        registry.register(enabled)
        registry.register(disabled)
        result = registry.get_enabled()
        assert len(result) == 1
        assert result[0].name == "on"

    def test_contains(self, registry, sample_heartbeat):
        registry.register(sample_heartbeat)
        assert "server_check" in registry
        assert "nonexistent" not in registry
