"""
Heartbeat パッケージ — 定期チェック条件の定義・管理・実行

起動時に config/heartbeats/*.yaml を自動読み込みする。
"""
import logging

from ..utils.logging_config import FILE_ONLY_LOG_EXTRA

logger = logging.getLogger(__name__)

from .models import HeartbeatDefinition
from .registry import HeartbeatRegistry, get_heartbeat_registry, register_heartbeat
from .loader import load_all_heartbeats, save_heartbeat_to_yaml, delete_heartbeat_yaml

# パッケージインポート時にHeartbeatを自動読み込み
_loaded_heartbeats = load_all_heartbeats()
if _loaded_heartbeats:
    logger.info(
        "Heartbeat registry initialized (%s heartbeats registered)",
        len(_loaded_heartbeats),
        extra=FILE_ONLY_LOG_EXTRA,
    )

__all__ = [
    "HeartbeatDefinition",
    "HeartbeatRegistry",
    "get_heartbeat_registry",
    "register_heartbeat",
    "load_all_heartbeats",
    "save_heartbeat_to_yaml",
    "delete_heartbeat_yaml",
]
