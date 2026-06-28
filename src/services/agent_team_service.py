"""Agent Team configuration helpers."""

from __future__ import annotations

import copy
from typing import Any


AGENT_TEAM_PROVIDERS = {
    "openai",
    "openrouter",
    "gemini",
    "ollama",
    "openai_compatible_local",
    "sglang",
    "antigravity-cli",
    "claude-cli",
    "codex-cli",
}

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
    "gemini",
    "antigravity-cli",
    "claude-cli",
    "codex-cli",
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


def config_get(config: Any, key: str, default: Any = None) -> Any:
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


def _member_from_agent_team(config: Any, member_key: str) -> dict[str, Any] | None:
    member = config_get(config, f"agent_team.members.{member_key}", None)
    return member if isinstance(member, dict) else None


def _member_from_roster(config: Any, member_key: str) -> dict[str, Any] | None:
    raw_roster = config_get(config, "agent_team.roster", None)
    if not isinstance(raw_roster, list):
        return None
    key = _clean_member_key(member_key)
    for raw in raw_roster:
        if not isinstance(raw, dict):
            continue
        raw_key = _clean_member_key(
            raw.get("member_key")
            or raw.get("key")
            or raw.get("id")
            or raw.get("role")
        )
        raw_id = _clean_member_key(raw.get("id"))
        if raw_key == key or raw_id == key:
            return raw
    return None


def _member_has_explicit_field(config: Any, member_key: str, *fields: str) -> bool:
    for raw in (_member_from_agent_team(config, member_key), _member_from_roster(config, member_key)):
        if not isinstance(raw, dict):
            continue
        for field in fields:
            if str(raw.get(field) or "").strip():
                return True
    return False


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


def _legacy_roster(config: Any) -> list[dict[str, Any]]:
    roster: list[dict[str, Any]] = []
    for member_key in sorted(AGENT_TEAM_MEMBER_KEYS):
        defaults = _default_member_settings(member_key)
        legacy = _member_from_agent_team(config, member_key) or {}
        roster.append(_normalize_roster_item({**defaults, **legacy}, member_key))
    return roster


def agent_team_enabled(config: Any) -> bool:
    return bool(config_get(config, "agent_team.enabled", False))


def agent_team_confirm_prompt(config: Any) -> bool:
    return bool(config_get(config, "agent_team.confirm_prompt", True))


def agent_team_notify(config: Any) -> bool:
    return bool(config_get(config, "agent_team.notify", True))


def agent_team_roster(config: Any) -> list[dict[str, Any]]:
    raw_roster = config_get(config, "agent_team.roster", None)
    if isinstance(raw_roster, list) and raw_roster:
        normalized: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for raw in raw_roster:
            if not isinstance(raw, dict):
                continue
            raw_key = _clean_member_key(
                raw.get("member_key")
                or raw.get("key")
                or raw.get("id")
                or raw.get("role")
            )
            legacy = _member_from_agent_team(config, raw_key) or {}
            item = _normalize_roster_item({**raw, **legacy})
            if item["member_key"] in AGENT_TEAM_MEMBER_KEYS:
                seen_keys.add(item["member_key"])
            normalized.append(item)
        for member_key in sorted(AGENT_TEAM_MEMBER_KEYS - seen_keys):
            defaults = _default_member_settings(member_key)
            legacy = _member_from_agent_team(config, member_key) or {}
            normalized.append(_normalize_roster_item({**defaults, **legacy}, member_key))
        return normalized
    return _legacy_roster(config)


def agent_team_member_declared(config: Any, member_key: str) -> bool:
    key = _clean_member_key(member_key)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return False
    if _member_from_agent_team(config, key) is not None:
        return True
    return any(item["member_key"] == key for item in agent_team_roster(config))


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
    member = agent_team_member_settings(config, key)
    return agent_team_enabled(config) and bool(member.get("enabled", False))


def agent_team_member_for(config: Any, member_key: str) -> dict[str, str] | None:
    """Return a validated provider/model target for one Agent Team member."""
    key = _clean_member_key(member_key)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return None
    if not agent_team_member_enabled(config, key):
        return None

    member = agent_team_member_settings(config, key)
    provider = str(member.get("provider") or "").strip().lower()
    model = str(member.get("model") or "").strip()
    if provider not in AGENT_TEAM_PROVIDERS or not model:
        return None

    result = {"provider": provider, "model": model}
    mode = str(member.get("mode") or member.get("reasoning_effort") or "").strip()
    if mode and _member_has_explicit_field(config, key, "mode", "reasoning_effort"):
        result["mode"] = mode
        result["reasoning_effort"] = mode
    runner = str(member.get("runner") or "").strip()
    if runner and _member_has_explicit_field(config, key, "runner"):
        result["runner"] = runner
    return result


def agent_team_active_roster(config: Any) -> list[dict[str, Any]]:
    if not agent_team_enabled(config):
        return []
    return [item for item in agent_team_roster(config) if item.get("enabled")]


def agent_team_scalable_members(config: Any) -> list[dict[str, Any]]:
    return [
        item
        for item in agent_team_active_roster(config)
        if item.get("member_key") in SCALABLE_MEMBER_KEYS and item.get("scalable")
    ]


def agent_team_delegate_member(config: Any, role: str) -> dict[str, Any] | None:
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
    return agent_team_member_settings(config, clean_role)


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
) -> str:
    from .llm_model_catalog import default_llm_mode_for_options, reasoning_effort_options_for_model

    options = reasoning_effort_options_for_model(provider, model)
    if not options:
        return ""

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
) -> str:
    mode = resolve_agent_team_member_mode(
        config,
        member_key=member_key,
        provider=provider,
        model=model,
    )
    if not mode:
        return ""

    provider_id = str(provider or "").strip().lower()
    if provider_id == "openai":
        config_set(config, "openai.reasoning_effort", mode)
    elif provider_id == "codex-cli":
        config_set(config, "codex_cli.reasoning_effort", mode)
    elif provider_id == "claude-cli":
        config_set(config, "claude_cli.reasoning_effort", mode)
    elif client is not None and hasattr(client, "set_llm_mode"):
        client.set_llm_mode(mode)
    return mode


def agent_team_members_by_provider(config: Any) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for member in agent_team_roster(config):
        provider = str(member.get("provider") or "").strip()
        model = str(member.get("model") or "").strip()
        member_key = str(member.get("member_key") or member.get("id") or "").strip()
        if not provider or not model or not member_key:
            continue
        result.setdefault(provider, []).append(
            {
                "member_key": member_key,
                "model": model,
            }
        )
    return result
