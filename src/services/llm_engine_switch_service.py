"""Atomic LLM engine switch helper (staged config → client swap → persist)."""

from __future__ import annotations

import copy
import logging
from typing import Any

from ..services.execution_profile_service import (
    MANUAL_EXECUTION_PROFILE_ID,
    resolve_execution_main_route,
)
from ..services.llm_model_catalog import build_llm_mode_state

logger = logging.getLogger(__name__)

_MISSING_CONFIG_VALUE = object()


class ConfigOverlay:
    """Read-only config view with staged dotted-key overrides."""

    def __init__(self, base: Any, changes: dict[str, Any]):
        self._base = base
        self._changes = changes

    def get(self, key: str, default: Any = None) -> Any:
        parts = key.split(".")
        value = _MISSING_CONFIG_VALUE
        staged_ancestor_found = False

        for depth in range(len(parts), 0, -1):
            ancestor_key = ".".join(parts[:depth])
            if ancestor_key not in self._changes:
                continue
            staged_ancestor_found = True
            value = copy.deepcopy(self._changes[ancestor_key])
            for part in parts[depth:]:
                if not isinstance(value, dict) or part not in value:
                    value = _MISSING_CONFIG_VALUE
                    break
                value = value[part]
            break

        if value is _MISSING_CONFIG_VALUE and not staged_ancestor_found:
            base_value = self._base.get(key, _MISSING_CONFIG_VALUE)
            if base_value is not _MISSING_CONFIG_VALUE:
                value = copy.deepcopy(base_value)

        child_prefix = f"{key}."
        child_changes = sorted(
            (
                (changed_key[len(child_prefix) :], changed_value)
                for changed_key, changed_value in self._changes.items()
                if changed_key.startswith(child_prefix)
            ),
            key=lambda item: item[0].count("."),
        )
        if child_changes:
            if not isinstance(value, dict):
                value = {}
            for child_key, child_value in child_changes:
                _set_dotted_mapping_value(
                    value,
                    child_key,
                    copy.deepcopy(child_value),
                )

        return default if value is _MISSING_CONFIG_VALUE else value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def _set_dotted_mapping_value(
    mapping: dict[str, Any],
    key: str,
    value: Any,
) -> None:
    cursor = mapping
    parts = key.split(".")
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def _delete_dotted_mapping_value(mapping: dict[str, Any], key: str) -> None:
    cursor = mapping
    parts = key.split(".")
    parents: list[tuple[dict[str, Any], str]] = []
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            return
        parents.append((cursor, part))
        cursor = child
    cursor.pop(parts[-1], None)
    for parent, part in reversed(parents):
        child = parent.get(part)
        if isinstance(child, dict) and not child:
            parent.pop(part, None)
        else:
            break


def _delete_config_value(config: Any, key: str) -> None:
    for attribute in ("config", "values"):
        mapping = getattr(config, attribute, None)
        if isinstance(mapping, dict):
            _delete_dotted_mapping_value(mapping, key)
            return


def persist_config_changes(config: Any, changes: dict[str, Any]) -> None:
    if not changes:
        return

    live_mapping = getattr(config, "config", None)
    if isinstance(live_mapping, dict):
        from ..app_config_store import update_app_config_keys_sync

        if not update_app_config_keys_sync(changes):
            raise RuntimeError("Failed to persist LLM configuration")
        for key, value in changes.items():
            _set_dotted_mapping_value(live_mapping, key, copy.deepcopy(value))
        return

    previous_values = {
        key: config.get(key, _MISSING_CONFIG_VALUE) for key in changes
    }
    persisted_keys: list[str] = []
    try:
        for key, value in changes.items():
            if hasattr(config, "save_to_file"):
                if not config.save_to_file(key, value):
                    raise RuntimeError(f"Failed to persist {key}")
            else:
                config.set(key, value)
            persisted_keys.append(key)
    except Exception:
        for key in reversed(persisted_keys):
            old_value = previous_values[key]
            if old_value is _MISSING_CONFIG_VALUE:
                _delete_config_value(config, key)
                continue
            if hasattr(config, "save_to_file"):
                config.save_to_file(key, old_value)
            else:
                config.set(key, old_value)
        raise


def restore_llm_client(server: Any, old_client: Any) -> None:
    try:
        server.set_llm_client(old_client)
    except Exception:
        logger.exception("Failed to run hooks while restoring previous LLM client")
        server._llm_client = old_client


def resolve_execution_route_from_changes(
    config: Any,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the effective main route against staged config changes."""

    return resolve_execution_main_route(ConfigOverlay(config, changes))


def build_provider_model_config_changes(
    config: Any,
    *,
    provider: str,
    model: str,
    reasoning_effort: str | None = None,
    execution_profile_id: str | None = None,
    base_url: str | None = None,
    deployment: Any = None,
    extra_changes: dict[str, Any] | None = None,
    persist_manual_engine: bool = True,
) -> dict[str, Any]:
    """Build staged config changes for a provider/model switch."""

    config_changes: dict[str, Any] = dict(extra_changes or {})

    if not persist_manual_engine:
        return config_changes

    def _apply_config(key: str, next_value: Any) -> None:
        if deployment is not None and getattr(deployment, "fixed", False):
            return
        config_changes[key] = next_value

    _apply_config("llm_provider", provider)
    _apply_config("llm_model", model)
    _apply_config(
        "llm_selection_kind",
        "routing_profile" if provider == "routing-profile" else "static",
    )
    _apply_config(
        "routing_profile_id",
        "free-team" if provider == "routing-profile" else "",
    )
    provider_model_keys = {
        "sglang": "sglang.model",
        "ollama": "ollama.model",
        "openai_compatible_local": "openai_compatible_local.model",
        "openrouter": "openrouter.model",
        "codex-cli": "codex_cli.model",
        "claude-cli": "claude_cli.model",
        "antigravity-cli": "antigravity_cli.model",
        "grok-cli": "grok_cli.model",
        "gemini": "gemini.model",
        "openai": "openai.model",
        "kimi": "kimi.model",
        "deepseek": "deepseek.model",
        "deepinfra": "deepinfra.model",
    }
    model_key = provider_model_keys.get(provider)
    if model_key:
        _apply_config(model_key, model)

    if isinstance(base_url, str) and base_url.strip():
        if provider in {
            "ollama",
            "openrouter",
            "kimi",
            "deepseek",
            "deepinfra",
            "openai_compatible_local",
        }:
            _apply_config(f"{provider}.base_url", base_url.strip())
        elif provider == "sglang":
            _apply_config("sglang_base_url", base_url.strip())

    effort = str(reasoning_effort or "").strip()
    if effort:
        if provider == "codex-cli":
            _apply_config("codex_cli.reasoning_effort", effort)
        elif provider == "claude-cli":
            _apply_config("claude_cli.reasoning_effort", effort)
        elif provider == "kimi" and effort == "max":
            _apply_config("kimi.reasoning_effort", "max")
        elif provider == "deepseek" and effort in {"none", "high", "max"}:
            _apply_config("deepseek.reasoning_effort", effort)
        elif provider == "deepinfra" and effort in {"none", "low", "medium", "high"}:
            _apply_config("deepinfra.reasoning_effort", effort)

    # Global Execution Profile ids are leftover metadata only.  Writing them
    # here used to override Main; keep the leftover store inert.
    del execution_profile_id
    return config_changes


def build_manual_engine_config_changes(
    config: Any,
    *,
    provider: str,
    model: str,
    reasoning_effort: str | None = None,
    base_url: str | None = None,
    deployment: Any = None,
    extra_changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build persisted config changes for a manual engine switch."""

    return build_provider_model_config_changes(
        config,
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        execution_profile_id=MANUAL_EXECUTION_PROFILE_ID,
        base_url=base_url,
        deployment=deployment,
        extra_changes=extra_changes,
        persist_manual_engine=True,
    )


async def apply_llm_runtime_switch(
    server: Any,
    *,
    persist_changes: dict[str, Any],
    provider: str,
    model: str,
    reasoning_effort: str | None = None,
    deployment: Any = None,
    compensate_local_server_switch: Any = None,
    broadcast_request: Any = None,
) -> dict[str, Any]:
    """Apply a runtime provider/model switch without persisting llm_provider/model."""

    from ..llm.manager import create_llm_client_for_target

    provider = str(provider or "").strip()
    model = str(model or "").strip()
    if not provider or not model:
        raise ValueError("provider and model are required")

    effort = str(reasoning_effort or "").strip()
    mode_overlay = ConfigOverlay(
        server.config,
        {**persist_changes, "llm_provider": provider, "llm_model": model},
    )
    try:
        new_client = create_llm_client_for_target(
            server.config,
            provider=provider,
            model=model,
            effort=effort,
        )
        next_mode_state = build_llm_mode_state(mode_overlay, client=new_client)
        # Preserve an explicit no-mode state for known profiles that do not
        # advertise a reasoning/thinking contract.  The generic fast fallback
        # remains available for unknown external local-model profiles through
        # build_llm_mode_state itself, but must not be reintroduced here while
        # switching to a known non-reasoning profile.
        next_runtime_mode = str(next_mode_state.get("mode") or "").strip()
        if (
            next_mode_state.get("kind") == "response_mode"
            and hasattr(new_client, "set_llm_mode")
        ):
            new_client.set_llm_mode(next_runtime_mode)

        old_client = server._llm_client
        old_runtime_mode = server._current_llm_mode
        changes_to_persist = dict(persist_changes)
        changes_to_persist["llm_runtime_mode"] = next_runtime_mode

        try:
            server.set_llm_client(new_client)
        except Exception:
            restore_llm_client(server, old_client)
            raise
        try:
            persist_config_changes(server.config, changes_to_persist)
        except Exception:
            restore_llm_client(server, old_client)
            server._current_llm_mode = old_runtime_mode
            raise
    except Exception:
        if callable(compensate_local_server_switch):
            compensate_local_server_switch()
        raise

    server._current_llm_mode = next_runtime_mode

    if broadcast_request is not None:
        from ..api.broadcast_scope import broadcast_llm_state_change

        try:
            await broadcast_llm_state_change(
                server,
                broadcast_request,
                {
                    "type": "llm_engine_change",
                    "data": {"provider": provider, "model": model},
                },
            )
            await broadcast_llm_state_change(
                server,
                broadcast_request,
                {
                    "type": "llm_mode_change",
                    "data": next_mode_state,
                },
            )
        except Exception:
            logger.warning("Failed to broadcast LLM engine change", exc_info=True)

    logger.info("LLM engine switched to %s/%s", provider, model)
    return {
        "success": True,
        "provider": provider,
        "model": model,
        "runtime_mode": next_runtime_mode,
    }


async def apply_llm_engine_switch(
    server: Any,
    *,
    provider: str,
    model: str,
    reasoning_effort: str | None = None,
    execution_profile_id: str | None = None,
    config_changes: dict[str, Any] | None = None,
    deployment: Any = None,
    local_switch_in_progress: bool = False,
    compensate_local_server_switch: Any = None,
    broadcast_request: Any = None,
) -> dict[str, Any]:
    """Apply provider/model switch atomically with rollback on failure."""

    provider = str(provider or "").strip()
    model = str(model or "").strip()
    if not provider or not model:
        raise ValueError("provider and model are required")

    persist_changes = config_changes or build_manual_engine_config_changes(
        server.config,
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        deployment=deployment,
    )

    return await apply_llm_runtime_switch(
        server,
        persist_changes=persist_changes,
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        deployment=deployment,
        compensate_local_server_switch=compensate_local_server_switch,
        broadcast_request=broadcast_request,
    )


__all__ = [
    "ConfigOverlay",
    "apply_llm_engine_switch",
    "apply_llm_runtime_switch",
    "build_manual_engine_config_changes",
    "build_provider_model_config_changes",
    "persist_config_changes",
    "resolve_execution_route_from_changes",
    "restore_llm_client",
]
