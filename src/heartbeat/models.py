"""
Heartbeatシステム - データモデル

HeartbeatDefinition: 定期チェック条件の定義
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


HEARTBEAT_MODES = frozenset({"agent_check", "project_steward"})


def validate_heartbeat_mode(value: str) -> str:
    mode = str(value or "").strip()
    if mode not in HEARTBEAT_MODES:
        allowed = ", ".join(sorted(HEARTBEAT_MODES))
        raise ValueError(
            f"Invalid heartbeat mode '{mode}'. Allowed values: {allowed}"
        )
    return mode


@dataclass
class HeartbeatDefinition:
    """定期チェック条件の定義"""
    name: str
    description: str
    checklist: str
    mode: str = "agent_check"
    interval_minutes: int = 30
    enabled: bool = True
    active_hours: Optional[Dict[str, str]] = None
    notify_channel: str = "websocket"
    actions: List[Dict[str, Any]] = field(default_factory=list)
    source_path: Optional[str] = None

    def __post_init__(self) -> None:
        self.mode = validate_heartbeat_mode(self.mode)

    def to_dict(self) -> Dict[str, Any]:
        """API応答用にシリアライズ"""
        result = {
            "name": self.name,
            "description": self.description,
            "checklist": self.checklist,
            "mode": self.mode,
            "interval_minutes": self.interval_minutes,
            "enabled": self.enabled,
            "notify_channel": self.notify_channel,
            "actions": self.actions,
        }
        if self.active_hours:
            result["active_hours"] = self.active_hours
        return result
