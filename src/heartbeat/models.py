"""
Heartbeatシステム - データモデル

HeartbeatDefinition: 定期チェック条件の定義
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class HeartbeatDefinition:
    """定期チェック条件の定義"""
    name: str
    description: str
    checklist: str
    interval_minutes: int = 30
    enabled: bool = True
    active_hours: Optional[Dict[str, str]] = None
    notify_channel: str = "websocket"
    source_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """API応答用にシリアライズ"""
        result = {
            "name": self.name,
            "description": self.description,
            "checklist": self.checklist,
            "interval_minutes": self.interval_minutes,
            "enabled": self.enabled,
            "notify_channel": self.notify_channel,
        }
        if self.active_hours:
            result["active_hours"] = self.active_hours
        return result
