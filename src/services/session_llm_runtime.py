"""Bind conversation session LLM settings into request-scoped runtime state."""

from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass
from typing import Any

from ..llm.openai_compatible_local_profiles import managed_local_runtime_for_model
from .conversation_session_selection import (
    read_session_llm_settings,
    session_loaded_team_ids,
    session_main_route_override_for_binding,
)
from .execution_profile_service import resolve_execution_main_route
from .session_llm_runtime_context import (
    bind_session_agent_team_selection,
    bind_session_execution_profile_id,
    bind_session_main_route_override,
    reset_session_agent_team_selection,
    reset_session_execution_profile_id,
    reset_session_main_route_override,
    session_main_route_override,
)


@dataclass(frozen=True)
class SessionLlmRuntimeBinding:
    agent_team_token: Any
    main_route_token: Any
    execution_profile_token: Any
    loaded_team_token: Any | None = None


# Only these two MTP controls are user-owned/persistable.  Availability and
# artifact/status fields are computed by the managed llama.cpp resolver and
# are intentionally not copied into session overlays.
_SESSION_LLAMA_CPP_RUNTIME_KEYS = (
    "mtp_enabled",
)
_SESSION_LLAMA_CPP_COMPUTED_KEYS = (
    "profile_id",
    "mtp_model_path",
    "mtp_supported",
    "mtp_available",
    "mtp_status",
    "mtp_reason",
    "mtp_artifact_path",
    "mtp_resolved_model_path",
    "mtp_variant_model_path",
    "mtp_mode",
)

_SESSION_LLAMA_CPP_FREETOKEN_MASK_KEYS = (
    "executable",
    "model_path",
    "model_root",
    "model_alias",
    "host",
    "port",
    "context_size",
    "gpu_layers",
    "extra_args",
    "auto_start",
    "readiness_timeout",
    "reasoning_effort",
    *_SESSION_LLAMA_CPP_RUNTIME_KEYS,
    *_SESSION_LLAMA_CPP_COMPUTED_KEYS,
)


def _session_registry_key(*, user_id: str | None, session_id: str | None) -> str:
    clean_session = str(session_id or "").strip()
    if not clean_session:
        return ""
    clean_user = str(user_id or "").strip()
    return f"{clean_user}:{clean_session}" if clean_user else clean_session


def release_session_agent_team_registry(
    user_id: str | None,
    session_id: str | None,
) -> None:
    """Release the process-local manual Team projection for one session.

    The durable conversation row is the source of truth for manual Team
    selection.  This in-memory projection is therefore removed only after a
    successful durable delete.  Keep key construction in
    :func:`_session_registry_key` so the release path cannot drift from the
    restore/bind paths (and never clear another user's session).
    """

    session_key = _session_registry_key(user_id=user_id, session_id=session_id)
    if not session_key:
        return

    from ..llm.runtime_tool_registry import (
        _LOADED_AGENT_TEAM_IDS_BY_SESSION,
        _LOADED_AGENT_TEAM_IDS_LOCK,
    )

    with _LOADED_AGENT_TEAM_IDS_LOCK:
        _LOADED_AGENT_TEAM_IDS_BY_SESSION.pop(session_key, None)


def restore_session_agent_team_registry(
    user_id: str | None,
    session_id: str | None,
    settings: dict[str, Any] | None,
) -> None:
    """Restore per-session manual Team load registry from persisted settings."""
    from ..llm.runtime_tool_registry import _LOADED_AGENT_TEAM_IDS_BY_SESSION, _LOADED_AGENT_TEAM_IDS_LOCK

    session_key = _session_registry_key(user_id=user_id, session_id=session_id)
    if not session_key:
        return
    loaded_ids = frozenset(session_loaded_team_ids(settings or {}))
    with _LOADED_AGENT_TEAM_IDS_LOCK:
        _LOADED_AGENT_TEAM_IDS_BY_SESSION[session_key] = loaded_ids


def bind_session_llm_runtime(
    *,
    settings: dict[str, Any] | None,
    user_id: str | None,
    session_id: str | None,
    config: Any | None = None,
) -> SessionLlmRuntimeBinding:
    settings = settings if isinstance(settings, dict) else {}
    team_selection = settings.get("agent_team_selection")
    team_selection = team_selection if isinstance(team_selection, dict) else {}
    agent_team_token = bind_session_agent_team_selection(team_selection)
    main_route_token = bind_session_main_route_override(
        session_main_route_override_for_binding(settings),
    )
    execution_profile_id = str(settings.get("execution_profile_id") or "").strip()
    special = settings.get("special_routing") if isinstance(settings.get("special_routing"), dict) else {}
    if (
        str(team_selection.get("mode") or "auto").strip().lower() != "fixed"
        or str(special.get("routing_profile_id") or "").strip() == "free-team"
    ):
        execution_profile_id = ""
    execution_profile_token = bind_session_execution_profile_id(execution_profile_id)
    loaded_team_token = None
    session_key = _session_registry_key(user_id=user_id, session_id=session_id)
    if session_key:
        from ..llm.runtime_tool_registry import (
            _LOADED_AGENT_TEAM_IDS,
            _LOADED_AGENT_TEAM_IDS_BY_SESSION,
            _LOADED_AGENT_TEAM_IDS_LOCK,
        )

        loaded_ids = frozenset(session_loaded_team_ids(settings))
        with _LOADED_AGENT_TEAM_IDS_LOCK:
            _LOADED_AGENT_TEAM_IDS_BY_SESSION[session_key] = loaded_ids
        loaded_team_token = _LOADED_AGENT_TEAM_IDS.set(loaded_ids)
    return SessionLlmRuntimeBinding(
        agent_team_token=agent_team_token,
        main_route_token=main_route_token,
        execution_profile_token=execution_profile_token,
        loaded_team_token=loaded_team_token,
    )


def bind_session_llm_runtime_from_context(
    context: Any,
    *,
    user_id: str | None,
    session_id: str | None,
    config: Any | None = None,
) -> SessionLlmRuntimeBinding:
    return bind_session_llm_runtime(
        settings=read_session_llm_settings(context),
        user_id=user_id,
        session_id=session_id,
        config=config,
    )


def reset_session_llm_runtime(binding: SessionLlmRuntimeBinding | None) -> None:
    if binding is None:
        return
    reset_session_agent_team_selection(binding.agent_team_token)
    reset_session_main_route_override(binding.main_route_token)
    reset_session_execution_profile_id(binding.execution_profile_token)
    if binding.loaded_team_token is not None:
        from ..llm.runtime_tool_registry import _LOADED_AGENT_TEAM_IDS

        _LOADED_AGENT_TEAM_IDS.reset(binding.loaded_team_token)


def _client_provider_model(client: Any) -> tuple[str, str]:
    provider = str(getattr(client, "provider_label", "") or "").strip().lower()
    model = str(getattr(client, "model_name", "") or "").strip()
    config = getattr(client, "config", None)
    if not provider and config is not None and hasattr(config, "get"):
        provider = str(config.get("llm_provider", "") or "").strip().lower()
    if not model and config is not None and hasattr(config, "get"):
        model = str(config.get("llm_model", "") or "").strip()
    return provider, model


def _normalize_effort(value: Any) -> str:
    return str(value or "").strip().lower()


def _client_route_effort(client: Any) -> str:
    provider, model = _client_provider_model(client)
    config = getattr(client, "config", None)
    if config is None or not hasattr(config, "get"):
        return ""
    if (
        provider == "openai_compatible_local"
        and managed_local_runtime_for_model(config, model) == "freetoken"
    ):
        return ""
    key_by_provider = {
        "openai": "openai.reasoning_effort",
        "deepseek": "deepseek.reasoning_effort",
        "deepinfra": "deepinfra.reasoning_effort",
        "kimi": "kimi.reasoning_effort",
        "codex-cli": "codex_cli.reasoning_effort",
        "claude-cli": "claude_cli.reasoning_effort",
        "openai_compatible_local": "openai_compatible_local.llama_cpp.reasoning_effort",
    }
    key = key_by_provider.get(provider)
    if not key:
        return ""
    return _normalize_effort(config.get(key))


def _effective_client_effort(client: Any) -> str:
    get_effort = getattr(client, "_get_reasoning_effort", None)
    if callable(get_effort):
        resolved = get_effort()
        if resolved:
            return _normalize_effort(resolved)
    return _client_route_effort(client)


def _session_effort_override(config: Any) -> str | None:
    main_override = session_main_route_override()
    if not main_override:
        return None
    provider = str(main_override.get("provider") or "").strip().lower()
    model = str(main_override.get("model") or "").strip()
    if (
        provider == "openai_compatible_local"
        and model
        and managed_local_runtime_for_model(config, model) == "freetoken"
    ):
        return None
    effort = _normalize_effort(main_override.get("effort"))
    if effort:
        return effort
    if provider and model:
        return None
    return None


def session_route_differs_from_client(client: Any, config: Any) -> bool:
    route = resolve_execution_main_route(config)
    provider = str(route.get("provider") or "").strip().lower()
    model = str(route.get("model") or "").strip()
    if not provider or not model:
        return False
    current_provider, current_model = _client_provider_model(client)
    if provider != current_provider or model != current_model:
        return True
    override_effort = _session_effort_override(config)
    if override_effort is None:
        return False
    return override_effort != _effective_client_effort(client)


def _turn_client_provider(turn_client: Any) -> str:
    provider = getattr(turn_client, "provider_label", None)
    if isinstance(provider, str) and provider.strip():
        return provider.strip().lower()
    config = getattr(turn_client, "config", None)
    if config is not None and hasattr(config, "get"):
        value = config.get("llm_provider", "")
        if isinstance(value, str):
            return value.strip().lower()
    return ""


def _overlay_session_llama_cpp_mtp_runtime(turn_client: Any, model: str) -> None:
    """Carry MTP user settings into managed session targets only.

    ``create_llm_client_for_target`` already overlays the canonical runtime,
    but keeping this small session-boundary projection makes the ownership
    rule explicit and protects callers that provide a lightweight target
    client.  External ``local-model`` targets have all stale MTP keys removed.
    """

    config = getattr(turn_client, "config", None)
    if config is None or not hasattr(config, "get"):
        return
    target_model = str(model or "").strip()
    managed_runtime = managed_local_runtime_for_model(
        config,
        target_model,
    )
    local = config.get("openai_compatible_local", {})
    local = copy.deepcopy(local) if isinstance(local, dict) else {}
    local["model"] = target_model
    setter = getattr(config, "set", None)

    if managed_runtime == "freetoken":
        local["llama_cpp"] = {}
        if callable(setter):
            setter("openai_compatible_local", local)
            for key in _SESSION_LLAMA_CPP_FREETOKEN_MASK_KEYS:
                setter(
                    f"openai_compatible_local.llama_cpp.{key}",
                    None,
                )
        return

    llama_cpp = local.get("llama_cpp")
    llama_cpp = copy.deepcopy(llama_cpp) if isinstance(llama_cpp, dict) else {}
    if target_model.casefold() == "local-model":
        for key in (
            *_SESSION_LLAMA_CPP_RUNTIME_KEYS,
            *_SESSION_LLAMA_CPP_COMPUTED_KEYS,
        ):
            # TargetConfig falls back to the persisted mapping when a nested
            # override omits a key; explicit None is therefore required to
            # mask stale managed values on the external target.
            llama_cpp[key] = None
    else:
        try:
            from src.service_manager._local_llm_servers import _llama_cpp_settings

            resolved = _llama_cpp_settings(config, model=target_model)
        except Exception:
            resolved = {}
            # A failed target resolution must not leave the previous profile's
            # computed MTP paths/status in the request-scoped overlay.  Keep
            # the base config intact for diagnostics, but explicitly mask all
            # user-owned/computed MTP keys so a later retry cannot launch a
            # stale embedded variant.
            for key in (
                *_SESSION_LLAMA_CPP_RUNTIME_KEYS,
                *_SESSION_LLAMA_CPP_COMPUTED_KEYS,
            ):
                llama_cpp[key] = None
        for key in (
            *_SESSION_LLAMA_CPP_RUNTIME_KEYS,
            *_SESSION_LLAMA_CPP_COMPUTED_KEYS,
        ):
            if key in resolved:
                llama_cpp[key] = resolved[key]
    local["llama_cpp"] = llama_cpp
    if callable(setter):
        setter("openai_compatible_local", local)


def _session_managed_local_model_already_ready(
    config: Any,
    *,
    model: str,
    managed_runtime: str | None,
) -> bool:
    """Return whether a managed session target advertises its exact alias.

    Session target construction can run after a managed local server has
    already finished loading.  In that case the strict ensure path would
    repeat launch/conflict handling even though the endpoint is usable.  This
    probe intentionally performs only configuration resolution and a
    read-only ``/v1/models`` request; any missing, malformed, or failed probe
    is treated as *not ready* so the existing strict ensure path remains the
    source of truth for recovery.

    ``managed_runtime`` is supplied by the caller rather than inferred here
    so this helper cannot accidentally classify stale nested configuration as
    a managed target.  Model IDs are compared case-sensitively: a merely
    similar or case-folded alias must not bypass readiness validation.
    """

    selected_model = str(model or "").strip()
    if not selected_model or managed_runtime not in {"llama_cpp", "freetoken"}:
        return False

    try:
        # Import through the service-manager facade so its hot-core probes
        # remain monkeypatchable for tests and diagnostics.  The facade's
        # llama model-ID helper is the exact (case-preserving) /v1/models
        # probe; for FreeToken use the underlying generic helper directly.
        import src.service_manager as service_manager

        if managed_runtime == "freetoken":
            settings = service_manager._freetoken_settings(
                config,
                model=selected_model,
            )
            base_url = service_manager._freetoken_base_url(
                config,
                model=selected_model,
            )
            from src.service_manager._local_llm_servers import (
                _local_openai_model_ids_exact,
            )

            model_ids = _local_openai_model_ids_exact(base_url)
        else:
            settings = service_manager._llama_cpp_settings(
                config,
                model=selected_model,
            )
            base_url = service_manager._llama_cpp_base_url(
                config,
                model=selected_model,
            )
            model_ids = service_manager._llama_cpp_model_ids_exact(base_url)

        if not isinstance(settings, dict):
            return False
        expected_alias = str(settings.get("model_alias") or "").strip()
        if not expected_alias or isinstance(model_ids, str):
            return False
        return expected_alias in model_ids
    except Exception:
        # Readiness is an optimization only.  Keep the strict ensure path
        # when resolution/probing is inconclusive or the endpoint is absent.
        return False


def ensure_session_turn_local_server(turn_client: Any) -> None:
    """Prepare a managed local server for a session turn client.

    SimpleNamespace mocks with ``config`` / ``model_name`` are enough to
    reach ``ensure_openai_compatible_local_server``.  ``local-model`` and
    non-local providers are left untouched.
    """

    config = getattr(turn_client, "config", None)
    model_name = getattr(turn_client, "model_name", None)
    if config is None or not isinstance(model_name, str) or not model_name.strip():
        return
    if _turn_client_provider(turn_client) != "openai_compatible_local":
        return
    if model_name.strip().casefold() == "local-model":
        return

    selected_model = model_name.strip()
    managed_runtime = managed_local_runtime_for_model(
        config,
        selected_model,
    )
    from src.service_manager import ensure_openai_compatible_local_server

    if managed_runtime == "freetoken":
        from src.service_manager import (
            freetoken_managed_launch_configuration_error,
        )
        launch_error = freetoken_managed_launch_configuration_error(
            config,
            model=selected_model,
        )
        if launch_error:
            raise RuntimeError(
                "選択したsession用FreeToken runtimeを準備できません。 "
                f"{launch_error}"
            )
    else:
        from src.service_manager import (
            llama_cpp_managed_launch_configuration_error,
        )

        # Preserve the existing llama.cpp/session preflight for registered
        # GGUF profiles and custom managed llama-server targets.
        launch_error = llama_cpp_managed_launch_configuration_error(
            config,
            model=selected_model,
        )
        if launch_error:
            raise RuntimeError(
                "選択したsession用llama.cpp runtimeを準備できません。 "
                f"{launch_error}"
            )

    if _session_managed_local_model_already_ready(
        config,
        model=selected_model,
        managed_runtime=managed_runtime,
    ):
        return

    ensure_openai_compatible_local_server(
        config,
        model=selected_model,
        raise_on_launch_error=True,
        force_restart=False,
    )


def build_session_turn_client(source_client: Any, config: Any) -> Any:
    """Clone the active client for one turn using the bound session main route."""
    from ..llm.manager import create_llm_client_for_target

    route = resolve_execution_main_route(config)
    provider = str(route.get("provider") or "").strip().lower()
    model = str(route.get("model") or "").strip()
    effort = str(route.get("effort") or route.get("reasoning_effort") or "").strip()
    if (
        provider == "openai_compatible_local"
        and managed_local_runtime_for_model(config, model) == "freetoken"
    ):
        effort = ""
    turn_client = create_llm_client_for_target(
        config,
        provider=provider,
        model=model,
        effort=effort,
        provider_options={"ephemeral_session_client": True},
    )
    if provider == "openai_compatible_local":
        _overlay_session_llama_cpp_mtp_runtime(turn_client, model)
    for attr in (
        "current_session_id",
        "current_assistant_message_id",
        "session_user_id",
        "current_project_id",
        "current_include_project_context",
        "current_command_capabilities",
        "current_response_model",
        "current_tool_required",
        "_native_tools_enabled",
        "_system_prompt_override",
        "_isolated_system_prompt_override",
        "current_edit_message_id",
        "generation_policy",
        "planning_policy",
        "external_persistence_enabled",
        "character_name",
        "_privacy_session_context",
        "_privacy_project_metadata",
        "_loaded_history_session_id",
        "_provider_state",
        "_provider_state_mode",
        "_runtime_registry_project_id",
    ):
        if hasattr(source_client, attr):
            setattr(turn_client, attr, getattr(source_client, attr))
    source_history = getattr(source_client, "history_manager", None)
    if source_history is not None:
        # Production provider clients all use HistoryManager-compatible
        # objects. Lightweight embeddings/tests may expose only add_message;
        # never replace a fully initialized target HistoryManager with an
        # object that cannot satisfy the target's session-sync contract.
        required_history_methods = (
            "clear",
            "add_message",
            "get_all",
            "get_model_messages",
        )
        if all(
            callable(getattr(source_history, name, None))
            for name in required_history_methods
        ):
            turn_client.history_manager = source_history

    isolated_prompt = str(
        getattr(source_client, "_isolated_system_prompt_override", "") or ""
    ).strip()
    if isolated_prompt:
        set_isolated_system_prompt = getattr(
            turn_client,
            "set_isolated_system_prompt",
            None,
        )
        if callable(set_isolated_system_prompt):
            result = set_isolated_system_prompt(isolated_prompt)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise RuntimeError(
                    "Session turn target isolated system prompt setter is async"
                )
        elif hasattr(turn_client, "system_prompt"):
            turn_client.system_prompt = isolated_prompt
        else:
            raise RuntimeError(
                "Session turn target cannot install isolated system prompt"
            )

    # The registry contains provider/client-bound closures such as contextual
    # pack loaders and delegates.  Never transplant it from the long-lived
    # client into a different provider client.  Carry only the monotonic
    # conversation pack state into the target client's own ToolPackSession.
    try:
        from ..llm.tool_packs import tool_pack_session_for_client

        source_pack_session = tool_pack_session_for_client(source_client)
        turn_pack_session = tool_pack_session_for_client(turn_client)
        turn_pack_session.load_many(source_pack_session.loaded)
    except Exception:
        pass

    # character_name is copied above after the target constructor built its
    # initial AgentDefinition.  Rebuild through the target provider so model,
    # registry, tool closures, and character identity all belong to one client.
    rebuild_agent = getattr(turn_client, "_create_character_agent", None)
    if callable(rebuild_agent):
        turn_client.agent = rebuild_agent()

    ensure_session_turn_local_server(turn_client)
    return turn_client


__all__ = [
    "SessionLlmRuntimeBinding",
    "_client_provider_model",
    "bind_session_llm_runtime",
    "bind_session_llm_runtime_from_context",
    "build_session_turn_client",
    "reset_session_llm_runtime",
    "release_session_agent_team_registry",
    "restore_session_agent_team_registry",
    "session_route_differs_from_client",
]
