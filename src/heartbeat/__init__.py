"""
Heartbeat パッケージ — 定期チェック条件の定義・管理・実行

起動時に config/heartbeats/*.yaml を自動読み込みする。
"""
from .models import HeartbeatDefinition
from .registry import HeartbeatRegistry, get_heartbeat_registry, register_heartbeat
from .loader import load_all_heartbeats, save_heartbeat_to_yaml, delete_heartbeat_yaml

# パッケージインポート時にHeartbeatを自動読み込み
_loaded_heartbeats = load_all_heartbeats()
if _loaded_heartbeats:
    print(f"[Heartbeat] {len(_loaded_heartbeats)}個のHeartbeatを登録しました")

__all__ = [
    "HeartbeatDefinition",
    "HeartbeatRegistry",
    "get_heartbeat_registry",
    "register_heartbeat",
    "load_all_heartbeats",
    "save_heartbeat_to_yaml",
    "delete_heartbeat_yaml",
]
