"""Heartbeat 実行境界の検証ヘルパー。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

HEARTBEAT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# HTTP 経由では受け付けない。YAML 管理の declarative action のみ実行対象。
HTTP_BLOCKED_ACTION_TYPES = frozenset({"run_script", "run_skill", "webhook", "create_task"})

GENERIC_REQUEST_ERROR = "リクエストを処理できませんでした"


class HeartbeatSecurityError(ValueError):
    """Heartbeat 境界違反。"""


def validate_heartbeat_name(name: str) -> str:
    """Heartbeat 名を検証する。"""
    value = str(name or "").strip()
    if not value or not HEARTBEAT_NAME_RE.fullmatch(value):
        raise HeartbeatSecurityError("無効な Heartbeat 名です")
    return value


def resolve_heartbeat_yaml_path(
    name: str,
    directory: Path,
) -> Path:
    """管理ディレクトリ配下に収まる YAML パスを返す。"""
    safe_name = validate_heartbeat_name(name)
    base_dir = directory.resolve()
    candidate = (base_dir / f"{safe_name}.yaml").resolve()
    try:
        candidate.relative_to(base_dir)
    except ValueError as exc:
        raise HeartbeatSecurityError("無効な Heartbeat パスです") from exc
    return candidate


def filter_actions_for_http_persistence(
    actions: Optional[Iterable[Dict[str, Any]]],
) -> list[Dict[str, Any]]:
    """HTTP 保存時に危険 action を除外し、既存 YAML 由来のみ保持する。"""
    if not actions:
        return []
    kept: list[Dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "").strip()
        if action_type in HTTP_BLOCKED_ACTION_TYPES:
            continue
        if action_type == "notify":
            kept.append(action)
    return kept


def assert_declarative_action_allowed(action_type: str) -> None:
    """スケジュール実行時に許可する action type を検証する。"""
    allowed = {"notify", "webhook", "create_task", "run_skill", "run_script"}
    if action_type not in allowed:
        raise HeartbeatSecurityError(f"未対応の action type です: {action_type}")
