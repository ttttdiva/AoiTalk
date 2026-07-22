"""Agent Team configuration helpers."""

from __future__ import annotations

import copy
from typing import Any


AGENT_TEAM_PROVIDERS = {
    "openai",
    "openrouter",
    "kimi",
    "gemini",
    "ollama",
    "openai_compatible_local",
    "sglang",
    "antigravity-cli",
    "claude-cli",
    "codex-cli",
    "grok-cli",
}

MODEL_ROUTE_CLASS_BY_ROUTE = {
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

BUILTIN_AGENT_TEAM_MODEL_GROUPS = {
    "heavy": {"name": "高負荷", "effort_policy": "same"},
    "light": {"name": "軽量", "effort_policy": "lower"},
}
RESERVED_AGENT_TEAM_MODEL_GROUP_IDS = {"heavy", "light", "auto"}
AUTO_AGENT_TEAM_GROUP_ID = "auto"

MODEL_ROUTING_PROVIDERS = AGENT_TEAM_PROVIDERS | {
    "claude",
    "grok",
}

AGENT_HARNESS_PROVIDERS = {"codex-cli", "claude-cli"}

SCALABLE_MEMBER_KEYS = {
    "architect",
    "explorer",
    "implementer",
    "reviewer",
}

SPECIALIST_MEMBER_KEYS = {
    "utility",
    "media",
    "spotify",
    "scenario",
    "writing",
    "import",
}

SINGLETON_MEMBER_KEYS = SPECIALIST_MEMBER_KEYS | {
    "advanced_reasoning",
    "agent_harness",
}

AGENT_TEAM_MEMBER_KEYS = SCALABLE_MEMBER_KEYS | SINGLETON_MEMBER_KEYS

AGENT_TEAM_EXTERNAL_APPROVAL_PROVIDERS = {
    "openai",
    "openrouter",
    "kimi",
    "gemini",
    "antigravity-cli",
    "claude-cli",
    "codex-cli",
    "grok-cli",
}

AGENT_TEAM_MEMBER_LABELS = {
    "agent_team": "Agent Team",
    "advanced_reasoning": "高度推論",
    "architect": "設計",
    "explorer": "調査",
    "implementer": "実装",
    "reviewer": "レビュー",
    "utility": "ユーティリティ",
    "media": "メディア",
    "spotify": "Spotify",
    "scenario": "TRPG_GM",
    "writing": "執筆",
    "import": "シナリオ素材取り込み",
    "agent_harness": "作業エージェント",
}

AGENT_TEAM_DEFAULT_ROLES: dict[str, dict[str, Any]] = {
    "advanced_reasoning": {
        "enabled": False,
        "provider": "openai",
        "model": "gpt-4o",
        "mode": "medium",
        "role": "judge",
        "tools": [],
        "scalable": False,
        "default_instances": 0,
        "max_instances": 1,
    },
    "architect": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "medium",
        "role": "architect",
        "tools": ["workspace_read", "repo_map"],
        "scalable": True,
        "default_instances": 1,
        "max_instances": 2,
    },
    "explorer": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "fast",
        "role": "explorer",
        "tools": ["workspace_read", "repo_map"],
        "scalable": True,
        "default_instances": 1,
        "max_instances": 6,
    },
    "implementer": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "medium",
        "role": "worker",
        "tools": ["workspace_read", "repo_map"],
        "scalable": True,
        "default_instances": 1,
        "max_instances": 4,
    },
    "reviewer": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "medium",
        "role": "reviewer",
        "tools": ["workspace_read", "repo_map"],
        "scalable": True,
        "default_instances": 1,
        "max_instances": 4,
    },
    "utility": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "fast",
        "role": "utility",
        "tools": ["utility"],
        "scalable": False,
        "default_instances": 1,
        "max_instances": 1,
    },
    "media": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "fast",
        "role": "media",
        "tools": ["media"],
        "scalable": False,
        "default_instances": 1,
        "max_instances": 1,
    },
    "spotify": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "fast",
        "role": "spotify",
        "tools": ["spotify"],
        "scalable": False,
        "default_instances": 1,
        "max_instances": 1,
    },
    "scenario": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "fast",
        "role": "TRPG_GM",
        "tools": ["scenario"],
        "scalable": False,
        "default_instances": 1,
        "max_instances": 1,
    },
    "writing": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "fast",
        "role": "writing",
        "tools": ["writing"],
        "scalable": False,
        "default_instances": 1,
        "max_instances": 1,
    },
    "import": {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "fast",
        "role": "scenario_import",
        "tools": ["import"],
        "scalable": False,
        "default_instances": 1,
        "max_instances": 1,
    },
    "agent_harness": {
        "enabled": True,
        "provider": "codex-cli",
        "model": "gpt-5-codex",
        "mode": "medium",
        "role": "work_agent",
        "tools": ["codex_exec", "claude_code", "custom_command"],
        "runner": "codex_exec",
        "scalable": False,
        "default_instances": 1,
        "max_instances": 1,
    },
}


def _raw_config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value
    if hasattr(config, "get"):
        return config.get(key, default)
    return default


def config_get(config: Any, key: str, default: Any = None) -> Any:
    """無料Team選択中だけ専用Agent Team overlayを合成して読む。"""

    if key.startswith("agent_team."):
        provider = str(_raw_config_get(config, "llm_provider", "") or "").lower()
        model = str(_raw_config_get(config, "llm_model", "") or "").lower()
        if provider == "routing-profile" and model == "free-team":
            from .free_team_defaults import free_team_profile_template

            profile = free_team_profile_template()
            stored = _raw_config_get(config, "routing_profiles.free-team", {}) or {}
            if isinstance(stored, dict):
                stored_team = stored.get("agent_team")
                profile.update(
                    {key: value for key, value in stored.items() if key != "agent_team"}
                )
                if isinstance(stored_team, dict):
                    merged_team = dict(profile.get("agent_team") or {})
                    for section in ("model_groups", "members"):
                        stored_section = stored_team.get(section)
                        if isinstance(stored_section, dict):
                            merged_team[section] = {
                                **dict(merged_team.get(section) or {}),
                                **stored_section,
                            }
                    merged_team.update(
                        {
                            key: value
                            for key, value in stored_team.items()
                            if key not in {"model_groups", "members"}
                        }
                    )
                    profile["agent_team"] = merged_team
            if not bool(profile.get("enabled", True)):
                return _raw_config_get(config, key, default)
            if key == "agent_team.delegation_enabled":
                return bool(profile.get("agent_team_enabled", True))
            overlay = profile.get("agent_team") or {}
            value: Any = overlay
            for part in key.removeprefix("agent_team.").split("."):
                if not isinstance(value, dict) or part not in value:
                    return _raw_config_get(config, key, default)
                value = value[part]
            return value
    return _raw_config_get(config, key, default)


def config_set(config: Any, key: str, value: Any) -> None:
    if hasattr(config, "set"):
        config.set(key, value)
        return
    if isinstance(config, dict):
        parts = key.split(".")
        target = config
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        target[parts[-1]] = value


def _clean_member_key(member_key: str) -> str:
    return str(member_key or "").strip()


def _route_from_model_routing(config: Any, route: str) -> dict[str, Any] | None:
    route = _clean_member_key(route)
    raw = config_get(config, f"model_routing.overrides.{route}", None)
    return raw if isinstance(raw, dict) else None


def _group_route_by_id(config: Any, group_id: str) -> dict[str, Any] | None:
    """Resolve a group_id from the single Agent Team model-group store."""
    group_id = str(group_id or "").strip()
    if not group_id:
        return None
    raw = config_get(config, f"agent_team.model_groups.{group_id}", None)
    if isinstance(raw, dict):
        return raw
    return None


def agent_team_member_configured_group_id(config: Any, member_key: str) -> str:
    """Return the persisted group ID, preserving explicit main inheritance."""
    key = _clean_member_key(member_key)
    member = config_get(config, f"agent_team.members.{key}", {}) or {}
    if isinstance(member, dict) and "group_id" in member:
        return str(member.get("group_id") or "").strip()
    return MODEL_ROUTE_CLASS_BY_ROUTE.get(key, "")


def agent_team_member_group_id(
    config: Any,
    member_key: str,
    *,
    delegation_group_id: str | None = None,
) -> str:
    """Return the effective group ID for a concrete execution."""
    configured = agent_team_member_configured_group_id(config, member_key)
    if configured != AUTO_AGENT_TEAM_GROUP_ID:
        return configured
    selected = str(delegation_group_id or "").strip()
    return selected if selected in BUILTIN_AGENT_TEAM_MODEL_GROUPS else ""


def _model_group_route(
    config: Any,
    route: str,
    *,
    delegation_group_id: str | None = None,
) -> dict[str, Any] | None:
    group_id = agent_team_member_group_id(
        config,
        route,
        delegation_group_id=delegation_group_id,
    )
    return _group_route_by_id(config, group_id) if group_id else None


def _main_route(config: Any) -> dict[str, Any]:
    provider = str(config_get(config, "llm_provider", "openai") or "openai").strip().lower()
    model = str(config_get(config, "llm_model", "") or "").strip()
    if not model:
        provider_model_keys = {
            "openai": ("openai.model",),
            "gemini": ("gemini.model",),
            "openrouter": ("openrouter.model",),
            "kimi": ("kimi.model",),
            "ollama": ("ollama.model",),
            "sglang": ("sglang.model",),
            "openai_compatible_local": ("openai_compatible_local.model",),
            "codex-cli": ("codex_cli.model",),
            "claude-cli": ("claude_cli.model",),
            "antigravity-cli": ("antigravity_cli.model",),
            "grok-cli": ("grok_cli.model",),
        }
        for key in provider_model_keys.get(provider, ()):
            model = str(config_get(config, key, "") or "").strip()
            if model:
                break
    return {"provider": provider, "model": model}


def _provider_configured(config: Any, provider: str, route: dict[str, Any], *, main: bool = False) -> bool:
    provider = str(provider or "").strip().lower()
    if not provider:
        return False
    if main:
        return True
    if provider in {"codex-cli", "claude-cli", "antigravity-cli", "grok-cli"}:
        return True
    if str(route.get("api_key") or "").strip() or str(route.get("base_url") or "").strip():
        return True
    if provider in {"openai", "gemini", "openrouter", "kimi", "grok", "claude"}:
        env_names = {
            "openai": ("OPENAI_API_KEY", "openai_api_key"),
            "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "gemini_api_key"),
            "openrouter": ("OPENROUTER_API_KEY", "openrouter_api_key"),
            "kimi": ("MOONSHOT_API_KEY", "kimi_api_key"),
            "grok": ("XAI_API_KEY", "xai_api_key", "grok_api_key"),
            "claude": ("ANTHROPIC_API_KEY", "anthropic_api_key"),
        }.get(provider, ())
        import os

        if any(os.getenv(name) for name in env_names if name.isupper()):
            return True
        if any(config_get(config, name, None) for name in env_names if not name.isupper()):
            return True
    if provider in {"ollama", "sglang", "openai_compatible_local"}:
        return bool(
            config_get(config, f"{provider}.base_url", None)
            or config_get(config, f"{provider}.host", None)
            or config_get(config, f"{provider}.model", None)
        )
    return False


def _normalize_route_target(config: Any, raw: dict[str, Any] | None, *, route: str, main: bool = False) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    provider = str(raw.get("provider") or "").strip().lower()
    model = str(raw.get("model") or "").strip()
    if route == "agent_harness":
        provider = provider or "codex-cli"
        model = model or "gpt-5-codex"
    static_target = provider in MODEL_ROUTING_PROVIDERS and bool(model)
    if not static_target:
        target_type = str(raw.get("target_type") or "").strip().lower()
        if target_type == "pool" or (
            provider == "routing-profile" and model == "free-team"
        ):
            pool_id = str(raw.get("pool_id") or "").strip()
            if not pool_id:
                pool_id = str(
                    config_get(
                        config,
                        "routing_profiles.free-team.main_pool_id",
                        "coordinator",
                    )
                    or "coordinator"
                )
            result = {
                "kind": "pool",
                "provider": "routing-profile",
                "model": "free-team",
                "routing_profile_id": str(
                    raw.get("routing_profile_id") or "free-team"
                ),
                "pool_id": pool_id,
            }
            effort_policy = str(raw.get("effort_policy") or "").strip()
            effort = str(
                raw.get("effort") or raw.get("reasoning_effort") or ""
            ).strip()
            if effort_policy:
                result["effort_policy"] = effort_policy
            if effort:
                result["effort"] = effort
                result["reasoning_effort"] = effort
            return result
        return None
    if route == "agent_harness" and provider not in AGENT_HARNESS_PROVIDERS:
        return None
    if not _provider_configured(config, provider, raw, main=main):
        return None
    result: dict[str, str] = {"provider": provider, "model": model}
    mode = str(raw.get("mode") or raw.get("reasoning_effort") or "").strip()
    if mode:
        result["mode"] = mode
        result["reasoning_effort"] = mode
    runner = str(raw.get("runner") or "").strip()
    if route == "agent_harness":
        if not runner:
            runner = "claude_code" if provider == "claude-cli" else "codex_exec"
        result["runner"] = runner
    elif runner:
        result["runner"] = runner
    return result


def _merge_route_layer(
    base: dict[str, Any],
    layer: dict[str, Any] | None,
) -> dict[str, Any]:
    """Overlay one route while treating blank target fields as inheritance."""
    merged = dict(base)
    for field, value in (layer or {}).items():
        if field in {"provider", "model", "runner"} and not str(value or "").strip():
            continue
        merged[field] = value
    return merged


def resolve_model_route(
    config: Any,
    route: str,
    *,
    delegation_group_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve one route by member override, selected/fixed group, then main."""
    key = _clean_member_key(route)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return None
    member = config_get(config, f"agent_team.members.{key}", {}) or {}
    member_override = member.get("override") if isinstance(member, dict) else None
    if key == "agent_harness":
        return _normalize_route_target(
            config,
            member_override or _route_from_model_routing(config, key) or {"provider": "codex-cli", "model": "gpt-5-codex", "runner": "codex_exec"},
            route=key,
        )
    main_route = _main_route(config)
    group_route = _model_group_route(
        config,
        key,
        delegation_group_id=delegation_group_id,
    ) or {}
    legacy_or_override = member_override if isinstance(member_override, dict) else (_route_from_model_routing(config, key) or {})
    merged_group = _merge_route_layer(main_route, group_route)
    merged_override = _merge_route_layer(merged_group, legacy_or_override)
    for raw, is_main in ((merged_override, not bool(group_route or legacy_or_override)), (merged_group, not bool(group_route)), (main_route, True)):
        target = _normalize_route_target(config, raw, route=key, main=is_main)
        if target is not None:
            return target
    return None


def model_route_is_explicit(
    config: Any,
    route: str,
    *,
    delegation_group_id: str | None = None,
) -> bool:
    """Return whether a route resolved from model_routing rather than main defaults."""
    key = _clean_member_key(route)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return False
    member = agent_team_member_configured(config, key)
    if isinstance(member.get("override"), dict) and any(member["override"].get(field) for field in ("provider", "model", "effort_policy")):
        return True
    group_id = agent_team_member_group_id(
        config,
        key,
        delegation_group_id=delegation_group_id,
    )
    if group_id and isinstance(_group_route_by_id(config, group_id), dict):
        return True
    override_target = _normalize_route_target(
        config,
        _route_from_model_routing(config, key),
        route=key,
    )
    if override_target is not None:
        return True
    group_target = _normalize_route_target(
        config,
        _model_group_route(
            config,
            key,
            delegation_group_id=delegation_group_id,
        ),
        route=key,
    )
    return group_target is not None


def _default_member_settings(member_key: str) -> dict[str, Any]:
    base = copy.deepcopy(AGENT_TEAM_DEFAULT_ROLES.get(member_key, {}))
    if not base:
        return {}
    base.setdefault("id", member_key)
    base.setdefault("member_key", member_key)
    base.setdefault("label", AGENT_TEAM_MEMBER_LABELS.get(member_key, member_key))
    base.setdefault("reasoning_effort", base.get("mode", "medium"))
    return base


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_roster_item(raw: dict[str, Any], fallback_key: str | None = None) -> dict[str, Any]:
    key = _clean_member_key(
        raw.get("member_key")
        or raw.get("key")
        or raw.get("id")
        or raw.get("role")
        or fallback_key
    )
    if key not in AGENT_TEAM_MEMBER_KEYS:
        key = fallback_key or key

    default = _default_member_settings(key) if key in AGENT_TEAM_MEMBER_KEYS else {}
    item = {**default, **copy.deepcopy(raw)}
    item["member_key"] = key
    item["id"] = str(item.get("id") or key)
    item["label"] = str(item.get("label") or AGENT_TEAM_MEMBER_LABELS.get(key, key))
    item["enabled"] = _normalize_bool(item.get("enabled"), default.get("enabled", False))

    provider = str(item.get("provider") or default.get("provider") or "").strip().lower()
    item["provider"] = provider
    item["model"] = str(item.get("model") or default.get("model") or "").strip()
    mode = str(
        item.get("mode")
        or item.get("reasoning_effort")
        or default.get("mode")
        or "medium"
    ).strip()
    item["mode"] = mode
    item["reasoning_effort"] = mode
    item["role"] = str(item.get("role") or default.get("role") or key).strip()
    tools = item.get("tools", default.get("tools", []))
    item["tools"] = [str(tool) for tool in tools] if isinstance(tools, list) else []
    item["scalable"] = _normalize_bool(item.get("scalable"), bool(default.get("scalable")))
    max_default = _normalize_int(default.get("max_instances"), 1)
    max_instances = max(1, _normalize_int(item.get("max_instances"), max_default))
    if not item["scalable"]:
        max_instances = 1
    item["max_instances"] = max_instances
    default_instances = _normalize_int(
        item.get("default_instances"),
        _normalize_int(default.get("default_instances"), 1 if item["enabled"] else 0),
    )
    item["default_instances"] = max(0, min(default_instances, max_instances))
    item["spawn_policy"] = str(item.get("spawn_policy") or "adaptive").strip()
    item["runner"] = str(item.get("runner") or default.get("runner") or "").strip()
    return item


def agent_team_enabled(config: Any) -> bool:
    return agent_team_delegation_enabled(config)


def agent_team_delegation_enabled(config: Any) -> bool:
    """委譲ツール（作業系サブエージェント）の公開スイッチ。デフォルトOFF。"""
    return _normalize_bool(config_get(config, "agent_team.delegation_enabled", False), False)


def agent_team_member_configured(config: Any, member_key: str) -> dict[str, Any]:
    raw = config_get(config, f"agent_team.members.{_clean_member_key(member_key)}", {}) or {}
    return raw if isinstance(raw, dict) else {}


def agent_team_confirm_prompt(config: Any) -> bool:
    return bool(config_get(config, "agent_team.confirm_prompt", True))


def agent_team_notify(config: Any) -> bool:
    return bool(config_get(config, "agent_team.notify", True))


def agent_team_roster(config: Any) -> list[dict[str, Any]]:
    roster: list[dict[str, Any]] = []
    for member_key in sorted(AGENT_TEAM_MEMBER_KEYS):
        defaults = _default_member_settings(member_key)
        member_config = agent_team_member_configured(config, member_key)
        override = {
            **member_config,
            **(member_config.get("override") or _route_from_model_routing(config, member_key) or {}),
        }
        target = resolve_model_route(config, member_key)
        item = _normalize_roster_item({**defaults, **override}, member_key)
        item["enabled"] = _normalize_bool(member_config.get("enabled"), defaults.get("enabled", False))
        item["group_id"] = agent_team_member_configured_group_id(config, member_key)
        if target:
            item.update(target)
        if member_key in SCALABLE_MEMBER_KEYS:
            item["scalable"] = True
            item["max_instances"] = max(
                1,
                _normalize_int(
                    override.get("max_instances"),
                    _normalize_int(defaults.get("max_instances"), 1),
                ),
            )
            item["default_instances"] = max(
                1,
                min(
                    _normalize_int(
                        override.get("default_instances"),
                        _normalize_int(defaults.get("default_instances"), 1),
                    ),
                    item["max_instances"],
                ),
            )
        roster.append(item)
    return roster


def agent_team_member_declared(config: Any, member_key: str) -> bool:
    key = _clean_member_key(member_key)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return False
    return bool(agent_team_member_configured(config, key))


def agent_team_member_settings(config: Any, member_key: str) -> dict[str, Any]:
    key = _clean_member_key(member_key)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return {}
    for item in agent_team_roster(config):
        if item.get("member_key") == key or item.get("id") == key:
            return item
    return _normalize_roster_item(_default_member_settings(key), key)


def agent_team_member_enabled(config: Any, member_key: str) -> bool:
    key = _clean_member_key(member_key)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return False
    configured = _normalize_bool(
        agent_team_member_configured(config, key).get("enabled"),
        _default_member_settings(key).get("enabled", False),
    )
    if key in SPECIALIST_MEMBER_KEYS:
        return configured
    return agent_team_delegation_enabled(config) and configured


def agent_team_member_for(
    config: Any,
    member_key: str,
    *,
    delegation_group_id: str | None = None,
) -> dict[str, str] | None:
    """Return a validated provider/model target for one Agent Team member."""
    key = _clean_member_key(member_key)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return None
    if not agent_team_member_enabled(config, key):
        return None
    return resolve_model_route(
        config,
        key,
        delegation_group_id=delegation_group_id,
    )


def agent_team_active_roster(config: Any) -> list[dict[str, Any]]:
    if not agent_team_delegation_enabled(config):
        return []
    return [item for item in agent_team_roster(config) if item.get("enabled")]


def agent_team_scalable_members(config: Any) -> list[dict[str, Any]]:
    return [
        item
        for item in agent_team_active_roster(config)
        if item.get("member_key") in SCALABLE_MEMBER_KEYS and item.get("scalable")
    ]


def agent_team_delegate_member(
    config: Any,
    role: str,
    *,
    delegation_group_id: str | None = None,
) -> dict[str, Any] | None:
    clean_role = _clean_member_key(role)
    if clean_role not in AGENT_TEAM_MEMBER_KEYS:
        alias = {
            "design": "architect",
            "research": "explorer",
            "implementation": "implementer",
            "review": "reviewer",
        }.get(clean_role)
        clean_role = alias or clean_role
    if clean_role not in AGENT_TEAM_MEMBER_KEYS:
        return None
    if not agent_team_member_enabled(config, clean_role):
        return None
    member = agent_team_member_settings(config, clean_role)
    configured_group_id = agent_team_member_configured_group_id(config, clean_role)
    effective_group_id = agent_team_member_group_id(
        config,
        clean_role,
        delegation_group_id=delegation_group_id,
    )
    target = resolve_model_route(
        config,
        clean_role,
        delegation_group_id=delegation_group_id,
    )
    if target:
        member.update(target)
        mode = resolve_agent_team_member_mode(
            config,
            member_key=clean_role,
            provider=str(target.get("provider") or ""),
            model=str(target.get("model") or ""),
            delegation_group_id=delegation_group_id,
        )
        member["mode"] = mode
        member["reasoning_effort"] = mode
    member["configured_group_id"] = configured_group_id
    member["group_id"] = effective_group_id or configured_group_id
    return member


def agent_team_clamp_instances(config: Any, role: str, requested: Any) -> int:
    member = agent_team_delegate_member(config, role)
    if not member:
        return 0
    default_instances = _normalize_int(member.get("default_instances"), 1)
    max_instances = max(1, _normalize_int(member.get("max_instances"), 1))
    count = _normalize_int(requested, default_instances)
    if not member.get("scalable"):
        return 1
    return max(1, min(count, max_instances))


def agent_team_member_requires_external_approval(
    member: dict[str, str] | None,
) -> bool:
    if not member:
        return False
    provider = str(member.get("provider") or "").strip().lower()
    return provider in AGENT_TEAM_EXTERNAL_APPROVAL_PROVIDERS


def agent_team_member_mode(
    config: Any,
    member_key: str,
    default: str = "",
) -> str:
    route = resolve_model_route(config, member_key)
    if route:
        mode = str(route.get("mode") or route.get("reasoning_effort") or "").strip()
        if mode:
            return mode
    member = agent_team_member_settings(config, member_key)
    return str(
        member.get("mode")
        or member.get("reasoning_effort")
        or default
        or ""
    ).strip()


def resolve_agent_team_member_mode(
    config: Any,
    *,
    member_key: str,
    provider: str,
    model: str,
    delegation_group_id: str | None = None,
) -> str:
    from .llm_model_catalog import default_llm_mode_for_options, reasoning_effort_options_for_model

    options = reasoning_effort_options_for_model(provider, model)
    if not options:
        return ""

    member = agent_team_member_configured(config, member_key)
    override = member.get("override") if isinstance(member.get("override"), dict) else {}
    group = _model_group_route(
        config,
        member_key,
        delegation_group_id=delegation_group_id,
    ) or {}
    if not member and not group:
        mode = agent_team_member_mode(config, member_key)
        return mode if mode in options else default_llm_mode_for_options(options)
    policy_source = override if override.get("effort_policy") else group
    policy = str(policy_source.get("effort_policy") or "same").strip().lower()
    if policy in {"default", "none"}:
        return ""
    if policy == "explicit":
        selected = str(policy_source.get("effort") or policy_source.get("reasoning_effort") or "").strip()
        return selected if selected in options else ""
    main = _main_route(config)
    main_key = {"openai": "openai.reasoning_effort", "kimi": "kimi.reasoning_effort", "codex-cli": "codex_cli.reasoning_effort", "claude-cli": "claude_cli.reasoning_effort"}.get(main.get("provider", ""), "")
    main_mode_value = (
        config_get(config, main_key, "")
        if main_key
        else config_get(config, "llm_runtime_mode", "")
    )
    main_mode = str(main_mode_value or "").strip()
    if main_mode not in options:
        main_mode = default_llm_mode_for_options(options)
    if policy == "lower":
        return options[max(0, options.index(main_mode) - 1)] if main_mode in options else ""
    if policy == "same":
        return main_mode if main_mode in options else ""
    mode = agent_team_member_mode(config, member_key)
    if mode not in options:
        mode = default_llm_mode_for_options(options)
    return mode


def apply_agent_team_member_mode(
    config: Any,
    *,
    member_key: str,
    provider: str,
    model: str,
    client: Any = None,
    delegation_group_id: str | None = None,
) -> str:
    mode = resolve_agent_team_member_mode(
        config,
        member_key=member_key,
        provider=provider,
        model=model,
        delegation_group_id=delegation_group_id,
    )
    if mode and client is not None and hasattr(client, "set_llm_mode"):
        client.set_llm_mode(mode)
    return mode


def agent_team_members_by_provider(config: Any) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    configured: dict[str, Any] = {}
    legacy = config_get(config, "model_routing.overrides", {}) or {}
    if isinstance(legacy, dict):
        configured.update(legacy)
    members = config_get(config, "agent_team.members", {}) or {}
    if isinstance(members, dict):
        for key, member in members.items():
            if isinstance(member, dict) and isinstance(member.get("override"), dict):
                configured[str(key)] = member["override"]
    groups = config_get(config, "agent_team.model_groups", {}) or {}
    if isinstance(groups, dict):
        configured.update({f"group:{key}": value for key, value in groups.items()})
    for member_key, member in configured.items():
        if not isinstance(member, dict):
            continue
        provider = str(member.get("provider") or "").strip()
        model = str(member.get("model") or "").strip()
        member_key = str(member_key or "").strip()
        if not provider or not model or not member_key:
            continue
        result.setdefault(provider, []).append(
            {
                "member_key": member_key,
                "model": model,
            }
        )
    return result
