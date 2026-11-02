"""Runtime helpers for the additive AgentRevision v2 routing fields.

P2 adds three nullable JSON columns to ``agent_revisions``.  This module keeps
the runtime reader independent from the ORM migration.  A missing (or
explicitly ``NULL``) field is deliberately different from an empty JSON
value: ``NULL`` identifies a historical revision, while an empty value is an
explicit v2 policy. Historical revisions remain readable but are no longer
eligible for new runtime execution.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any


_MISSING = object()

# The P2 role names are deliberately small and stable.  Work-role aliases are
# retained so old callers can ask for a Subagent while a v2 revision resolves
# the corresponding role route.
WORK_ROLE_TO_MODEL_ROLE: dict[str, str] = {
    "chat": "chat",
    "planner": "research",
    "researcher": "research",
    "writer": "chat",
    "data_worker": "chat",
    "general_worker": "chat",
    "operator": "chat",
    "explorer": "code",
    "code_worker": "code",
    "reviewer": "review",
    "summarizer": "summarize",
    "summarize": "summarize",
    "code": "code",
    "research": "research",
    "review": "review",
}

# This is the compatibility projection used when an old Team/Subagent call
# reaches a v2 revision.  The first role is the canonical role for a legacy
# Subagent.  The resolver still applies the revision's tool/authority gates.
LEGACY_SUBAGENT_TO_MODEL_ROLE: dict[str, str] = {
    "architecture_planner": "research",
    "general_researcher": "research",
    "code_explorer": "code",
    "code_implementer": "code",
    "code_reviewer": "review",
    "general_worker": "chat",
    "employee_operator": "chat",
    "docs_operator": "chat",
    "workspace_operator": "chat",
    "project_operator": "chat",
    "personal_private_adapter": "chat",
    "ui_qa_worker": "review",
}

_MODEL_ROLE_ALIASES = {
    "main": "chat",
    "conversation": "chat",
    "worker": "chat",
    "planner": "research",
    "researcher": "research",
    "explorer": "code",
    "code_worker": "code",
    "reviewer": "review",
    "summarizer": "summarize",
}


def _read(revision: Any, *names: str) -> Any:
    """Read one of the migration's accepted ORM/mapping spellings."""

    for name in names:
        if isinstance(revision, Mapping):
            if name in revision:
                return revision[name]
            continue
        try:
            value = getattr(revision, name)
        except AttributeError:
            continue
        return value
    return _MISSING


def revision_model_overrides(revision: Any) -> dict[str, Any] | None:
    """Return a detached v2 ``model_overrides`` object, or ``None`` for NULL.

    ``model_overrides_json`` is accepted for ORM models that keep the public
    property name as a compatibility alias.  The returned value is detached
    so callers cannot mutate an immutable revision or a frozen binding.
    """

    value = _read(revision, "model_overrides", "model_overrides_json")
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, Mapping):
        return {}
    return copy.deepcopy(dict(value))


def revision_tool_ids(revision: Any) -> list[str] | None:
    """Return the explicit v2 tool allowlist, preserving NULL vs ``[]``."""

    value = _read(revision, "tool_ids", "tool_ids_json")
    if value is _MISSING or value is None:
        return None
    if isinstance(value, str):
        raw: Sequence[Any] = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = list(value)
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        clean = str(item or "").strip().casefold()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def revision_delegate_policy(revision: Any) -> dict[str, Any] | None:
    """Return a detached v2 ``delegate_policy`` object, or ``None`` for NULL."""

    value = _read(revision, "delegate_policy", "delegate_policy_json")
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, Mapping):
        return {}
    return copy.deepcopy(dict(value))


def revision_v2_fields_present(revision: Any) -> bool:
    """Whether any additive v2 field is explicitly present on a revision."""

    return any(
        value is not None
        for value in (
            revision_model_overrides(revision),
            revision_tool_ids(revision),
            revision_delegate_policy(revision),
        )
    )


def model_role_for_work_role(role: Any) -> str:
    clean = str(role or "").strip().casefold()
    return WORK_ROLE_TO_MODEL_ROLE.get(clean, _MODEL_ROLE_ALIASES.get(clean, clean))


def model_role_for_legacy_subagent(subagent_id: Any) -> str | None:
    clean = str(subagent_id or "").strip().casefold()
    return LEGACY_SUBAGENT_TO_MODEL_ROLE.get(clean)


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        value: Any = config
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return value
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, default)
        except TypeError:
            value = getter(key)
        return default if value is None else value
    return default


def _clean(value: Any) -> str:
    return "" if value is _MISSING else str(value or "").strip()


def _route_items(value: Any) -> list[Any]:
    """Read a route block without silently turning NULL into a route."""

    if isinstance(value, Mapping):
        if "routes" in value:
            raw = value.get("routes")
        elif any(key in value for key in ("kind", "provider", "model", "pool_id")):
            raw = [value]
        else:
            raw = []
    else:
        raw = value
    if isinstance(raw, Mapping):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def _route_from_legacy_main(
    config: Any,
    main_route_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project the existing Main write-through settings to a route object."""

    override = main_route_override if isinstance(main_route_override, Mapping) else None
    provider = _clean(
        override.get("provider") if override is not None else _config_get(config, "llm_provider", "openai")
    ).casefold()
    model = _clean(
        override.get("model") if override is not None else _config_get(config, "llm_model", "")
    )
    selection_kind = _clean(_config_get(config, "llm_selection_kind", "")).casefold()
    if provider == "routing-profile" or model == "free-team" or selection_kind == "routing_profile":
        pool_id = _clean(
            _config_get(config, "routing_profiles.free-team.main_pool_id", "coordinator")
        ) or "coordinator"
        return {
            "kind": "pool",
            "pool_id": pool_id,
            "provider": "routing-profile",
            "model": "free-team",
        }

    if not provider:
        provider = "openai"
    if not model:
        # Keep the canonical provider-aware resolver as the fallback for a
        # sparse config.  It also supplies the catalog-aligned effort value.
        try:
            from .model_roles_service import resolve_execution_main_route

            resolved = dict(resolve_execution_main_route(config))
            provider = _clean(resolved.get("provider")).casefold() or provider
            model = _clean(resolved.get("model"))
            effort = _clean(resolved.get("effort") or resolved.get("reasoning_effort"))
        except Exception:
            effort = ""
    else:
        effort = ""
        effort_keys = {
            "openai": "openai.reasoning_effort",
            "deepseek": "deepseek.reasoning_effort",
            "deepinfra": "deepinfra.reasoning_effort",
            "kimi": "kimi.reasoning_effort",
            "codex-cli": "codex_cli.reasoning_effort",
            "claude-cli": "claude_cli.reasoning_effort",
            "openai_compatible_local": "openai_compatible_local.llama_cpp.reasoning_effort",
        }
        if override is not None:
            effort = _clean(override.get("effort") or override.get("reasoning_effort"))
        elif effort_keys.get(provider):
            effort = _clean(_config_get(config, effort_keys[provider], ""))
    route: dict[str, Any] = {
        "kind": "llm",
        "provider": provider,
        "model": model,
    }
    if effort:
        route["effort"] = effort
    return route


def _global_role_block(config: Any, role: str) -> Mapping[str, Any] | None:
    roles = _config_get(config, "model_roles.roles", {})
    if not isinstance(roles, Mapping):
        return None
    value = roles.get(role)
    return value if isinstance(value, Mapping) else None


def _global_chat_routes(
    config: Any,
    main_route_override: Mapping[str, Any] | None = None,
) -> tuple[list[Any], bool]:
    primary = _route_from_legacy_main(config, main_route_override)
    block = _global_role_block(config, "chat") or {}
    fallback_value = block.get("fallbacks", [])
    if fallback_value is None:
        fallback_value = []
    if not isinstance(fallback_value, (list, tuple)):
        return [primary], False
    return [primary, *list(fallback_value)], block.get("allow_paid_fallback") is True


def _global_role_routes(
    config: Any,
    model_role: str,
    main_route_override: Mapping[str, Any] | None = None,
) -> tuple[list[Any], bool, str]:
    chat_routes, chat_allow_paid = _global_chat_routes(config, main_route_override)
    if model_role == "chat":
        return chat_routes, chat_allow_paid, "global"
    block = _global_role_block(config, model_role)
    if block is not None and "routes" in block:
        routes = _route_items(block)
        if routes:
            return routes, block.get("allow_paid_fallback") is True, "global"
    # P1 role rows that are empty inherit the complete Chat route chain.
    return chat_routes, chat_allow_paid, "inherit_chat"


def _override_block(overrides: Mapping[str, Any], model_role: str) -> tuple[Any, bool]:
    for key in (model_role, *[alias for alias, target in WORK_ROLE_TO_MODEL_ROLE.items() if target == model_role]):
        if key in overrides:
            return overrides[key], True
    return None, False


def _normalize_route(config: Any, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("agent route is not an object")
    kind = _clean(raw.get("kind")).casefold()
    if kind == "pool":
        pool_id = _clean(raw.get("pool_id"))
        if not pool_id:
            raise ValueError("agent pool route is missing pool_id")
        return {
            "kind": "pool",
            "pool_id": pool_id,
            "provider": "routing-profile",
            "model": "free-team",
        }
    provider = _clean(raw.get("provider")).casefold()
    model = _clean(raw.get("model"))
    if not provider or not model:
        raise ValueError("agent route is missing provider/model")
    if provider == "muse-remote" and model != "muse-chat":
        raise ValueError("muse-remote routes require model muse-chat")
    result: dict[str, Any] = {
        "kind": "llm",
        "provider": provider,
        "model": model,
    }
    effort = _clean(raw.get("effort") or raw.get("reasoning_effort"))
    if effort:
        result["effort"] = effort
    return result


def _acting_subagent_id(revision: Any, model_role: str, role: str) -> str:
    allowed = _read(revision, "allowed_subagent_ids")
    values = allowed if isinstance(allowed, (list, tuple, set, frozenset)) else ()
    preferred = {
        "research": ("architecture_planner", "general_researcher", "general_worker", "employee_operator"),
        "code": ("code_explorer", "code_implementer", "workspace_operator", "general_worker", "employee_operator"),
        "review": ("code_reviewer", "general_researcher", "architecture_planner", "employee_operator"),
        "chat": ("employee_operator", "general_worker", "docs_operator", "workspace_operator", "employee_operator"),
    }.get(model_role, ())
    allowed_clean = {str(item).strip() for item in values if str(item).strip()}
    for candidate in preferred:
        if candidate in allowed_clean:
            return candidate
    return str(role or model_role).strip() or model_role


def revision_role_fallback_policy(revision: Any, role: Any) -> tuple[str, ...] | None:
    """Read only the pinned revision's own role policy."""
    from .agent_route_fallback_policy import normalize_fallback_on

    model_role = model_role_for_work_role(role)
    if model_role not in {"chat", "summarize", "code", "review", "research"}:
        raise ValueError("unknown agent role")
    overrides = revision_model_overrides(revision)
    if overrides is None:
        return None
    block, present = _override_block(overrides, model_role)
    if not present or not isinstance(block, Mapping):
        return None
    return normalize_fallback_on(block.get("fallback_on"))


def revision_role_route_candidates(
    config: Any,
    revision: Any,
    role: Any,
    *,
    main_route_override: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool, str]:
    """Return normalized candidate routes and fallback policy for one role."""

    model_role = model_role_for_work_role(role)
    if model_role not in {"chat", "summarize", "code", "review", "research"}:
        raise ValueError("unknown agent role")
    overrides = revision_model_overrides(revision)
    if overrides is not None:
        block, present = _override_block(overrides, model_role)
        if present:
            routes = _route_items(block)
            if not routes:
                raise ValueError("agent revision override has no routes")
            if isinstance(block, Mapping) and isinstance(
                block.get("allow_paid_fallback"), bool
            ):
                allow_paid = block["allow_paid_fallback"]
            else:
                _, allow_paid, _ = _global_role_routes(
                    config,
                    model_role,
                    main_route_override,
                )
            source = "agent"
        else:
            routes, allow_paid, source = _global_role_routes(
                config, model_role, main_route_override
            )
            chat_block, chat_present = _override_block(overrides, "chat")
            if source == "inherit_chat" and chat_present and _route_items(chat_block):
                # An empty role inherits this Agent's chat model, not the
                # global one, when the Agent overrides chat.
                return revision_role_route_candidates(
                    config, revision, "chat", main_route_override=main_route_override
                )[0:2] + ("inherit_chat",)
    else:
        routes, allow_paid, source = _global_role_routes(
            config, model_role, main_route_override
        )
    normalized = [_normalize_route(config, route) for route in routes]
    if not normalized:
        raise ValueError("agent role has no routes")
    return normalized, allow_paid, source


def revision_chat_route_candidates(
    config: Any,
    revision: Any,
    *,
    main_route_override: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool, str]:
    """Return Chat candidates for the request-local LLM fallback boundary."""

    return revision_role_route_candidates(
        config,
        revision,
        "chat",
        main_route_override=main_route_override,
    )


def _strict_route(route: Mapping[str, Any]) -> dict[str, Any]:
    """Project one primary route to the contract's provider-neutral shape."""

    if route.get("kind") == "pool":
        pool_id = _clean(route.get("pool_id"))
        if not pool_id:
            raise ValueError("agent pool route is missing pool_id")
        return {"kind": "pool", "pool_id": pool_id}
    provider = _clean(route.get("provider")).casefold()
    model = route.get("model")
    model_text = _clean(model)
    if not provider or not model_text:
        raise ValueError("agent route is missing provider/model")
    result: dict[str, Any] = {"kind": "llm", "provider": provider, "model": model_text}
    effort = _clean(route.get("effort") or route.get("reasoning_effort"))
    if effort:
        result["effort"] = effort
    return result


def resolve_agent_role_route_v2(
    config: Any,
    revision: Any,
    role: Any,
    *,
    main_route_override: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve the first v2 route for ``role``.

    All-NULL v2 fields are historical revisions and are no longer executable;
    callers receive ``(None, "legacy")`` and may display the legacy pins.
    Otherwise the first candidate is returned in the strict provider-neutral
    route shape and the source is one of ``agent``, ``global`` or
    ``inherit_chat``.
    """
    role_name = _clean(role).casefold()
    if not role_name:
        from .agent_model_routing import AgentModelRouteError

        raise AgentModelRouteError("agent_route_role_unknown")
    if model_role_for_work_role(role_name) not in {
        "chat",
        "summarize",
        "code",
        "review",
        "research",
    }:
        from .agent_model_routing import AgentModelRouteError

        raise AgentModelRouteError("agent_route_role_unknown")
    from .agent_model_routing import AgentModelRouteError, ROUTABLE_PROVIDERS

    if not revision_v2_fields_present(revision):
        return None, "legacy"

    try:
        candidates, _allow_paid, source = revision_role_route_candidates(
            config,
            revision,
            role_name,
            main_route_override=main_route_override,
        )
    except ValueError as exc:
        raise AgentModelRouteError("agent_route_model_unavailable") from exc
    primary = candidates[0]
    provider = _clean(primary.get("provider")).casefold()
    if provider not in ROUTABLE_PROVIDERS:
        raise AgentModelRouteError("agent_route_provider_unsupported")
    try:
        strict = _strict_route(primary)
    except ValueError as exc:
        raise AgentModelRouteError("agent_route_model_unavailable") from exc
    return strict, source


__all__ = [
    "LEGACY_SUBAGENT_TO_MODEL_ROLE",
    "WORK_ROLE_TO_MODEL_ROLE",
    "model_role_for_legacy_subagent",
    "model_role_for_work_role",
    "resolve_agent_role_route_v2",
    "revision_chat_route_candidates",
    "revision_delegate_policy",
    "revision_model_overrides",
    "revision_role_route_candidates",
    "revision_role_fallback_policy",
    "revision_tool_ids",
    "revision_v2_fields_present",
]
