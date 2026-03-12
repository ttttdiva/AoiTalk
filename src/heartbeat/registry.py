"""
Heartbeatシステム - レジストリ

HeartbeatRegistry: Heartbeat定義のシングルトン管理
"""
import logging
from typing import Dict, List, Optional

from .models import HeartbeatDefinition

logger = logging.getLogger(__name__)


class HeartbeatRegistry:
    """Heartbeat定義のレジストリ"""

    def __init__(self):
        self._heartbeats: Dict[str, HeartbeatDefinition] = {}

    def register(self, heartbeat: HeartbeatDefinition) -> None:
        self._heartbeats[heartbeat.name] = heartbeat
        logger.info(f"[HeartbeatRegistry] 登録: {heartbeat.name}")

    def unregister(self, name: str) -> bool:
        if name in self._heartbeats:
            del self._heartbeats[name]
            return True
        return False

    def get(self, name: str) -> Optional[HeartbeatDefinition]:
        return self._heartbeats.get(name)

    def get_all(self) -> List[HeartbeatDefinition]:
        return list(self._heartbeats.values())

    def get_enabled(self) -> List[HeartbeatDefinition]:
        return [h for h in self._heartbeats.values() if h.enabled]

    def __contains__(self, name: str) -> bool:
        return name in self._heartbeats

    def __len__(self) -> int:
        return len(self._heartbeats)


_global_heartbeat_registry = HeartbeatRegistry()


def get_heartbeat_registry() -> HeartbeatRegistry:
    return _global_heartbeat_registry


def register_heartbeat(heartbeat: HeartbeatDefinition) -> None:
    _global_heartbeat_registry.register(heartbeat)
