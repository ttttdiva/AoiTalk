"""Database-backed application configuration storage."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .config_defaults import load_default_config
from .security.field_crypto import (
    decrypt_json_secret_leaves,
    encrypt_json_secret_leaves,
)


logger = logging.getLogger(__name__)
GLOBAL_CONFIG_KEY = "global"
OBSOLETE_AGENT_TEAM_MEMBER_KEYS = {
    "search",
    "filesystem",
    "project_management",
    "skills",
}


def _load_legacy_yaml(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Failed to read legacy config YAML %s: %s", path, exc)
        return None


def _fill_missing_defaults(value: Any, defaults: Any) -> Any:
    """Return value with missing keys filled from defaults, preserving user values."""
    if not isinstance(value, dict) or not isinstance(defaults, dict):
        return copy.deepcopy(value)

    merged = copy.deepcopy(value)
    for key, default_value in defaults.items():
        if key not in merged:
            merged[key] = copy.deepcopy(default_value)
        elif isinstance(merged[key], dict) and isinstance(default_value, dict):
            merged[key] = _fill_missing_defaults(merged[key], default_value)
    return merged


def _prune_obsolete_app_config(value: Dict[str, Any]) -> Dict[str, Any]:
    """Drop stale config branches that no longer have a runtime owner."""
    cleaned = copy.deepcopy(value)
    cleaned.pop("model_sharing", None)

    agent_team = cleaned.get("agent_team")
    if isinstance(agent_team, dict):
        members = agent_team.get("members")
        if isinstance(members, dict):
            for member_key in OBSOLETE_AGENT_TEAM_MEMBER_KEYS:
                members.pop(member_key, None)
        roster = agent_team.get("roster")
        if isinstance(roster, list):
            agent_team["roster"] = [
                item
                for item in roster
                if not (
                    isinstance(item, dict)
                    and str(
                        item.get("member_key")
                        or item.get("key")
                        or item.get("id")
                        or ""
                    )
                    in OBSOLETE_AGENT_TEAM_MEMBER_KEYS
                )
            ]

    agents = cleaned.get("agents")
    if isinstance(agents, dict):
        agents.pop("skills", None)

    return cleaned


def _migrate_agent_team_to_model_routing(value: Dict[str, Any]) -> Dict[str, Any]:
    """Move legacy agent_team model choices into model_routing.overrides."""
    migrated = copy.deepcopy(value)
    agent_team = migrated.get("agent_team")
    if isinstance(migrated.get("model_routing"), dict):
        if isinstance(agent_team, dict):
            agent_team.pop("enabled", None)
            agent_team.pop("members", None)
            agent_team.pop("roster", None)
        return migrated

    overrides: Dict[str, Any] = {}
    if isinstance(agent_team, dict):
        members = agent_team.get("members")
        if isinstance(members, dict):
            for key, member in members.items():
                if not isinstance(member, dict):
                    continue
                provider = str(member.get("provider") or "").strip()
                model = str(member.get("model") or "").strip()
                if not provider or not model:
                    continue
                route = {"provider": provider, "model": model}
                for field in ("mode", "reasoning_effort", "max_instances", "runner"):
                    if field in member and member.get(field) not in (None, ""):
                        route[field] = member.get(field)
                overrides[str(key)] = route
        agent_team.pop("enabled", None)
        agent_team.pop("members", None)
        agent_team.pop("roster", None)

    migrated["model_routing"] = {
        "classes": {
            "heavy": {},
            "light": {},
            "vision": {"provider": "", "model": "", "base_url": "", "api_key": ""},
            "audio": {
                "engine": "speech_recognition",
                "provider": "",
                "model": "",
                "base_url": "",
                "api_key": "",
            },
        },
        "media": {"image_mode": "auto"},
        "overrides": overrides,
    }
    return migrated


def _db_deps():
    from sqlalchemy.exc import SQLAlchemyError

    from .memory.database import get_database_manager
    from .memory.models import AppConfigSetting

    return SQLAlchemyError, get_database_manager, AppConfigSetting


def _ensure_table() -> None:
    _, get_database_manager, AppConfigSetting = _db_deps()
    db_manager = get_database_manager()
    AppConfigSetting.__table__.create(bind=db_manager.sync_engine, checkfirst=True)


def load_app_config_sync(legacy_config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the global app config from DB, seeding it on first use."""

    seed = (
        _load_legacy_yaml(legacy_config_path)
        if legacy_config_path is not None
        else None
    ) or load_default_config()
    seed = _migrate_agent_team_to_model_routing(_prune_obsolete_app_config(seed))

    try:
        SQLAlchemyError, get_database_manager, AppConfigSetting = _db_deps()
        _ensure_table()
        db_manager = get_database_manager()
        with db_manager.get_sync_session() as session:
            row = session.get(AppConfigSetting, GLOBAL_CONFIG_KEY)
            if row is None:
                row = AppConfigSetting(
                    key=GLOBAL_CONFIG_KEY,
                    value=encrypt_json_secret_leaves(
                        copy.deepcopy(seed),
                        aad_prefix="app_config_settings.value",
                    ),
                )
                session.add(row)
                session.commit()
                logger.info("Seeded app configuration into database")
                return copy.deepcopy(seed)
            stored_value = row.value if isinstance(row.value, dict) else {}
            encrypted_value = encrypt_json_secret_leaves(
                stored_value,
                aad_prefix="app_config_settings.value",
            )
            if encrypted_value != stored_value:
                row.value = copy.deepcopy(encrypted_value)
                session.commit()
                stored_value = encrypted_value

            stored_decrypted = decrypt_json_secret_leaves(
                stored_value,
                aad_prefix="app_config_settings.value",
            )
            value = _migrate_agent_team_to_model_routing(
                _prune_obsolete_app_config(stored_decrypted)
            )
            merged = _fill_missing_defaults(value, seed)
            if merged != stored_decrypted:
                row.value = encrypt_json_secret_leaves(
                    copy.deepcopy(merged),
                    aad_prefix="app_config_settings.value",
                )
                session.commit()
            return copy.deepcopy(merged)
    except Exception as exc:
        logger.error("Failed to load app configuration from DB: %s", exc)
        return copy.deepcopy(seed)


def save_app_config_sync(config: Dict[str, Any]) -> bool:
    """Replace the global app config JSON in DB."""

    try:
        config = _migrate_agent_team_to_model_routing(_prune_obsolete_app_config(config))
        SQLAlchemyError, get_database_manager, AppConfigSetting = _db_deps()
        _ensure_table()
        db_manager = get_database_manager()
        with db_manager.get_sync_session() as session:
            row = session.get(AppConfigSetting, GLOBAL_CONFIG_KEY)
            if row is None:
                row = AppConfigSetting(
                    key=GLOBAL_CONFIG_KEY,
                    value=encrypt_json_secret_leaves(
                        copy.deepcopy(config),
                        aad_prefix="app_config_settings.value",
                    ),
                )
                session.add(row)
            else:
                row.value = encrypt_json_secret_leaves(
                    copy.deepcopy(config),
                    aad_prefix="app_config_settings.value",
                )
            session.commit()
        return True
    except Exception as exc:
        logger.error("Failed to save app configuration to DB: %s", exc)
        return False


def update_app_config_key_sync(key: str, value: Any) -> bool:
    """Update one dotted config key in the stored DB JSON."""

    config = load_app_config_sync()
    current: Dict[str, Any] = config
    parts = key.split(".")
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value
    return save_app_config_sync(config)
