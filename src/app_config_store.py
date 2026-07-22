"""Database-backed application configuration storage."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .config_defaults import load_default_config
from .tts.irodori_config import normalize_irodori_settings
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

_BUILTIN_AGENT_TEAM_MODEL_GROUPS: Dict[str, Dict[str, Any]] = {
    "heavy": {
        "name": "高負荷",
        "provider": "",
        "model": "",
        "effort_policy": "same",
        "effort": "",
    },
    "light": {
        "name": "軽量",
        "provider": "",
        "model": "",
        "effort_policy": "lower",
        "effort": "",
    },
}

_DEFAULT_AGENT_TEAM_GROUP_BY_MEMBER = {
    "advanced_reasoning": "heavy",
    "architect": "heavy",
    "explorer": "light",
    "implementer": "light",
    "reviewer": "light",
    "utility": "light",
    "media": "light",
    "spotify": "light",
    "import": "light",
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

    tts_settings = cleaned.get("tts_settings")
    if isinstance(tts_settings, dict):
        irodori_settings = tts_settings.get("irodori_tts")
        if isinstance(irodori_settings, dict):
            normalize_irodori_settings(irodori_settings)

    return cleaned


# 旧 config_defaults が members に自動充填していた既定値。
# これと一致する値はユーザー設定ではないため overrides へ変換しない。
_LEGACY_AGENT_TEAM_DEFAULT_TARGETS: Dict[str, tuple] = {
    "architect": ("openai", "gpt-4o-mini"),
    "explorer": ("openai", "gpt-4o-mini"),
    "implementer": ("openai", "gpt-4o-mini"),
    "reviewer": ("openai", "gpt-4o-mini"),
    "utility": ("openai", "gpt-4o-mini"),
    "media": ("openai", "gpt-4o-mini"),
    "spotify": ("openai", "gpt-4o-mini"),
    "scenario": ("openai", "gpt-4o-mini"),
    "writing": ("openai", "gpt-4o-mini"),
    "import": ("openai", "gpt-4o-mini"),
    "advanced_reasoning": ("openai", "gpt-4o"),
    "agent_harness": ("codex-cli", "gpt-5-codex"),
}


def _normalize_legacy_model_group(
    group_id: str,
    value: Any,
) -> Dict[str, Any]:
    """Normalize one legacy class/model-group without dropping user fields."""
    default = _BUILTIN_AGENT_TEAM_MODEL_GROUPS[group_id]
    group = copy.deepcopy(value) if isinstance(value, dict) else {}
    legacy_effort = group.pop("reasoning_effort", None) or group.pop("mode", None)
    if legacy_effort and not group.get("effort_policy"):
        group["effort_policy"] = "explicit"
        group["effort"] = legacy_effort
    for key, default_value in default.items():
        group.setdefault(key, copy.deepcopy(default_value))
    # 既定グループの表示名は固定し、stable IDと常に対応させる。
    group["name"] = default["name"]
    return group


def _migrate_agent_team_to_model_routing(value: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Agent Team settings and migrate legacy heavy/light classes."""
    migrated = copy.deepcopy(value)
    agent_team = migrated.setdefault("agent_team", {})
    if not isinstance(agent_team, dict):
        agent_team = {}
        migrated["agent_team"] = agent_team
    if isinstance(migrated.get("model_routing"), dict):
        legacy_members = agent_team.get("members")
        if isinstance(legacy_members, dict) and not agent_team.get("member_settings_initialized"):
            normalized_members: Dict[str, Any] = {}
            for key, member in legacy_members.items():
                if not isinstance(member, dict) or key in OBSOLETE_AGENT_TEAM_MEMBER_KEYS:
                    continue
                existing_override = member.get("override")
                override = (
                    copy.deepcopy(existing_override)
                    if isinstance(existing_override, dict)
                    else {
                        field: member[field]
                        for field in ("provider", "model", "runner")
                        if member.get(field)
                    }
                )
                effort = member.get("reasoning_effort") or member.get("mode")
                if effort and not override.get("effort_policy"):
                    override.update({"effort_policy": "explicit", "effort": effort})
                normalized_members[key] = {
                    "enabled": bool(member.get("enabled", key != "advanced_reasoning")),
                    "group_id": str(
                        member.get("group_id")
                        if "group_id" in member
                        else _DEFAULT_AGENT_TEAM_GROUP_BY_MEMBER.get(str(key), "")
                    ),
                    "override": override,
                }
                for field in ("default_instances", "max_instances"):
                    if field in member:
                        normalized_members[key][field] = member[field]
            if normalized_members:
                agent_team["members"] = normalized_members
                agent_team["member_settings_initialized"] = True

        members = agent_team.get("members")
        if not isinstance(members, dict):
            members = {}
            agent_team["members"] = members
        legacy_overrides = migrated["model_routing"].get("overrides", {})
        migrated_legacy_override = False
        if isinstance(legacy_overrides, dict):
            for key, route in legacy_overrides.items():
                if (
                    not isinstance(route, dict)
                    or str(key) in OBSOLETE_AGENT_TEAM_MEMBER_KEYS
                    or str(key) not in _LEGACY_AGENT_TEAM_DEFAULT_TARGETS
                ):
                    continue
                member = members.setdefault(
                    str(key),
                    {
                        "enabled": str(key) != "advanced_reasoning",
                        "group_id": _DEFAULT_AGENT_TEAM_GROUP_BY_MEMBER.get(
                            str(key), ""
                        ),
                        "override": {},
                    },
                )
                if not isinstance(member, dict):
                    continue
                existing_override = member.get("override")
                if not isinstance(existing_override, dict) or not any(
                    existing_override.get(field)
                    for field in (
                        "provider",
                        "model",
                        "runner",
                        "effort_policy",
                        "effort",
                    )
                ):
                    override = {
                        field: copy.deepcopy(route[field])
                        for field in ("provider", "model", "runner")
                        if route.get(field)
                    }
                    legacy_effort = route.get("reasoning_effort") or route.get("mode")
                    if legacy_effort:
                        override.update(
                            {"effort_policy": "explicit", "effort": legacy_effort}
                        )
                    member["override"] = override
                    migrated_legacy_override = bool(override) or migrated_legacy_override
                for field in ("default_instances", "max_instances"):
                    if field not in member and field in route:
                        member[field] = copy.deepcopy(route[field])
                        migrated_legacy_override = True
        if migrated_legacy_override:
            agent_team["member_settings_initialized"] = True

        if isinstance(members, dict):
            for key, member in members.items():
                if (
                    isinstance(member, dict)
                    and "group_id" not in member
                    and str(key) in _DEFAULT_AGENT_TEAM_GROUP_BY_MEMBER
                ):
                    member["group_id"] = _DEFAULT_AGENT_TEAM_GROUP_BY_MEMBER[str(key)]

        classes = migrated["model_routing"].get("classes", {})
        classes = classes if isinstance(classes, dict) else {}
        existing_groups = agent_team.get("model_groups")
        groups = copy.deepcopy(existing_groups) if isinstance(existing_groups, dict) else {}
        for group_id in ("heavy", "light"):
            # 同じIDが両方にある場合はmodel_groupsを優先する。旧classは
            # 読み取り互換のため保存したままにするが、以後のruntime ownerにはしない。
            source = groups.get(group_id)
            if not isinstance(source, dict):
                source = classes.get(group_id)
            groups[group_id] = _normalize_legacy_model_group(group_id, source)
        agent_team["model_groups"] = groups
        agent_team.pop("enabled", None)
        agent_team.pop("roster", None)
        return migrated

    overrides: Dict[str, Any] = {}
    normalized_members: Dict[str, Any] = {}
    if isinstance(agent_team, dict):
        members = agent_team.get("members")
        if isinstance(members, dict):
            for key, member in members.items():
                if not isinstance(member, dict):
                    continue
                member_override = {field: member[field] for field in ("provider", "model", "runner") if member.get(field)}
                effort = member.get("reasoning_effort") or member.get("mode")
                if effort:
                    member_override.update({"effort_policy": "explicit", "effort": effort})
                normalized_members[str(key)] = {
                    "enabled": bool(member.get("enabled", str(key) != "advanced_reasoning")),
                    "group_id": str(
                        member.get("group_id")
                        if "group_id" in member
                        else _DEFAULT_AGENT_TEAM_GROUP_BY_MEMBER.get(str(key), "")
                    ),
                    "override": member_override,
                }
                provider = str(member.get("provider") or "").strip()
                model = str(member.get("model") or "").strip()
                if not provider or not model:
                    continue
                if _LEGACY_AGENT_TEAM_DEFAULT_TARGETS.get(str(key)) == (
                    provider.lower(),
                    model,
                ):
                    continue
                route = {"provider": provider, "model": model}
                for field in ("mode", "reasoning_effort", "max_instances", "runner"):
                    if field in member and member.get(field) not in (None, ""):
                        route[field] = member.get(field)
                overrides[str(key)] = route
        agent_team.pop("enabled", None)
        if normalized_members:
            agent_team["members"] = normalized_members
            agent_team["member_settings_initialized"] = True
        agent_team.pop("roster", None)

    migrated["model_routing"] = {
        "classes": {
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
    existing_groups = agent_team.get("model_groups")
    groups = copy.deepcopy(existing_groups) if isinstance(existing_groups, dict) else {}
    for group_id in ("heavy", "light"):
        groups[group_id] = _normalize_legacy_model_group(
            group_id,
            groups.get(group_id),
        )
    agent_team["model_groups"] = groups
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
