"""
Heartbeatシステム - YAMLローダー / セーバー

config/heartbeats/*.yaml からHeartbeat定義を読み込み・保存する。
"""
import logging
from pathlib import Path
from typing import List, Optional

import yaml

from .models import HeartbeatDefinition
from .registry import get_heartbeat_registry, register_heartbeat

logger = logging.getLogger(__name__)

HEARTBEATS_DIR = Path(__file__).resolve().parents[2] / "config" / "heartbeats"


def load_heartbeat_from_yaml(path: Path) -> Optional[HeartbeatDefinition]:
    """YAMLファイルからHeartbeatを1つ読み込む"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        return HeartbeatDefinition(
            name=data.get("name", path.stem),
            description=data.get("description", ""),
            checklist=data.get("checklist", ""),
            interval_minutes=data.get("interval_minutes", 30),
            enabled=data.get("enabled", True),
            active_hours=data.get("active_hours"),
            notify_channel=data.get("notify_channel", "websocket"),
            source_path=str(path),
        )
    except Exception as e:
        logger.error(f"[HeartbeatLoader] {path} の読み込みに失敗: {e}")
        return None


def load_all_heartbeats(heartbeats_dir: Optional[Path] = None) -> List[HeartbeatDefinition]:
    """Heartbeatディレクトリ内の全YAMLを読み込みレジストリに登録"""
    directory = heartbeats_dir or HEARTBEATS_DIR
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
        logger.info(f"[HeartbeatLoader] ディレクトリを作成: {directory}")
        return []

    heartbeats: List[HeartbeatDefinition] = []
    for yaml_file in sorted(directory.glob("*.yaml")):
        heartbeat = load_heartbeat_from_yaml(yaml_file)
        if heartbeat:
            register_heartbeat(heartbeat)
            heartbeats.append(heartbeat)

    logger.info(f"[HeartbeatLoader] {len(heartbeats)}個のHeartbeatを読み込みました")
    return heartbeats


def save_heartbeat_to_yaml(heartbeat: HeartbeatDefinition, heartbeats_dir: Optional[Path] = None) -> bool:
    """HeartbeatをYAMLファイルに保存"""
    directory = heartbeats_dir or HEARTBEATS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{heartbeat.name}.yaml"

    data = {
        "name": heartbeat.name,
        "description": heartbeat.description,
        "checklist": heartbeat.checklist,
        "interval_minutes": heartbeat.interval_minutes,
        "enabled": heartbeat.enabled,
        "notify_channel": heartbeat.notify_channel,
    }
    if heartbeat.active_hours:
        data["active_hours"] = heartbeat.active_hours

    try:
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        logger.info(f"[HeartbeatLoader] 保存: {heartbeat.name} -> {path}")
        return True
    except Exception as e:
        logger.error(f"[HeartbeatLoader] {heartbeat.name} の保存に失敗: {e}")
        return False


def delete_heartbeat_yaml(name: str, heartbeats_dir: Optional[Path] = None) -> bool:
    """Heartbeat YAMLファイルを削除"""
    directory = heartbeats_dir or HEARTBEATS_DIR
    path = directory / f"{name}.yaml"
    if path.exists():
        path.unlink()
        return True
    return False
