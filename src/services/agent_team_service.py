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


def _route_from_model_routing(config: Any, route: str) -> dict[str, Any] | None:
    route = _clean_member_key(route)
    raw = config_get(config, f"model_routing.overrides.{route}", None)
    return raw if isinstance(raw, dict) else None


def _class_route_from_model_routing(config: Any, route: str) -> dict[str, Any] | None:
    route_class = MODEL_ROUTE_CLASS_BY_ROUTE.get(_clean_member_key(route))
    if not route_class:
        return None
    raw = config_get(config, f"model_routing.classes.{route_class}", None)
    return raw if isinstance(raw, dict) else None


def _main_route(config: Any) -> dict[str, Any]:
    provider = str(config_get(config, "llm_provider", "openai") or "openai").strip().lower()
    model = str(config_get(config, "llm_model", "") or "").strip()
    if not model:
        provider_model_keys = {
            "openai": ("openai.model",),
            "gemini": ("gemini.model",),
            "openrouter": ("openrouter.model",),
            "ollama": ("ollama.model",),
            "sglang": ("sglang.model",),
            "openai_compatible_local": ("openai_compatible_local.model",),
            "codex-cli": ("codex_cli.model",),
            "claude-cli": ("claude_cli.model",),
            "antigravity-cli": ("antigravity_cli.model",),
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
    if provider in {"codex-cli", "claude-cli", "antigravity-cli"}:
        return True
    if str(route.get("api_key") or "").strip() or str(route.get("base_url") or "").strip():
        return True
    if provider in {"openai", "gemini", "openrouter", "grok", "claude"}:
        env_names = {
            "openai": ("OPENAI_API_KEY", "openai_api_key"),
            "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "gemini_api_key"),
            "openrouter": ("OPENROUTER_API_KEY", "openrouter_api_key"),
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


def _normalize_route_target(config: Any, raw: dict[str, Any] | None, *, route: str, main: bool = False) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    provider = str(raw.get("provider") or "").strip().lower()
    model = str(raw.get("model") or "").strip()
    if route == "agent_harness":
        provider = provider or "codex-cli"
        model = model or "gpt-5-codex"
    if provider not in MODEL_ROUTING_PROVIDERS or not model:
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


def resolve_model_route(config: Any, route: str) -> dict[str, str] | None:
    """Resolve one model route by override, class target, then main model."""
    key = _clean_member_key(route)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return None
    if key == "agent_harness":
        return _normalize_route_target(
            config,
            _route_from_model_routing(config, key) or {"provider": "codex-cli", "model": "gpt-5-codex", "runner": "codex_exec"},
            route=key,
        )
    for raw, is_main in (
        (_route_from_model_routing(config, key), False),
        (_class_route_from_model_routing(config, key), False),
        (_main_route(config), True),
    ):
        target = _normalize_route_target(config, raw, route=key, main=is_main)
        if target is not None:
            return target
    return None


def model_route_is_explicit(config: Any, route: str) -> bool:
    """Return whether a route resolved from model_routing rather than main defaults."""
    key = _clean_member_key(route)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return False
    override_target = _normalize_route_target(
        config,
        _route_from_model_routing(config, key),
        route=key,
    )
    if override_target is not None:
        return True
    class_target = _normalize_route_target(
        config,
        _class_route_from_model_routing(config, key),
        route=key,
    )
    return class_target is not None


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
    return True


def agent_team_confirm_prompt(config: Any) -> bool:
    return bool(config_get(config, "agent_team.confirm_prompt", True))


def agent_team_notify(config: Any) -> bool:
    return bool(config_get(config, "agent_team.notify", True))


def agent_team_roster(config: Any) -> list[dict[str, Any]]:
    roster: list[dict[str, Any]] = []
    for member_key in sorted(AGENT_TEAM_MEMBER_KEYS):
        defaults = _default_member_settings(member_key)
        override = _route_from_model_routing(config, member_key) or {}
        target = resolve_model_route(config, member_key)
        item = _normalize_roster_item({**defaults, **override}, member_key)
        item["enabled"] = target is not None
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
    return resolve_model_route(config, key) is not None


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
    return resolve_model_route(config, key) is not None


def agent_team_member_for(config: Any, member_key: str) -> dict[str, str] | None:
    """Return a validated provider/model target for one Agent Team member."""
    key = _clean_member_key(member_key)
    if key not in AGENT_TEAM_MEMBER_KEYS:
        return None
    return resolve_model_route(config, key)


def agent_team_active_roster(config: Any) -> list[dict[str, Any]]:
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
    overrides = config_get(config, "model_routing.overrides", {}) or {}
    if not isinstance(overrides, dict):
        return result
    for member_key, member in overrides.items():
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
