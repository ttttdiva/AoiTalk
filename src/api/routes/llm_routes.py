"""LLM モード/エンジン切替・モデルカタログ・Ollama モデル管理ルート (server.py から移設)"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ...services.llm_model_catalog import (
    _strata_static_catalog_settings,
    build_engine_options as build_llm_engine_options,
    build_llm_mode_state,
    build_model_catalog as build_llm_model_catalog,
    CACHEABLE_REMOTE_PROVIDERS,
    cached_provider_has_snapshot,
    cached_provider_is_fresh,
    cached_provider_updated_at,
    DEEPINFRA_MODEL_CATALOG_ERROR,
    DEEPINFRA_MODEL_CATALOG_RETRY_SECONDS,
    DEEPINFRA_MODEL_CATALOG_TTL_SECONDS,
    load_model_catalog_cache,
    save_model_catalog_cache,
    update_model_catalog_cache,
)
from ...services.agent_team_service import agent_team_orchestration_mode
from ...services.execution_profile_service import (
    execution_profile_envelope,
    resolve_execution_main_route,
)
from ...services.agent_team_v3 import agent_team_v3_teams
from ...services.conversation_session_selection import (
    merge_session_llm_settings,
    new_chat_llm_defaults_envelope,
    read_session_llm_settings,
    session_main_route_override_for_binding,
    session_llm_settings_envelope,
    validate_session_llm_settings,
)
from ...services.session_llm_runtime import restore_session_agent_team_registry
from ...services.provider_runtime_ownership import (
    ProviderRuntimeOwnership,
    provider_runtime_ownership,
)
from ...services.local_llm_paths import (
    canonicalize_llama_cpp_model_root_override,
    resolve_llama_cpp_model_root,
)
from ...features import Features
from ...llm.openrouter_provider_routing import (
    MODEL_PROVIDER_OPTIONS_CONFIG_KEY,
    fetch_provider_candidates,
    normalize_model_provider_options,
    normalize_provider_options,
    provider_options_for_model,
)
from ...llm.deployment_resolver import (
    DeploymentMismatchError,
    preflight_deployment,
    resolve_llm_deployment,
)
from ...llm.openai_compatible_local_profiles import (
    llama_cpp_auxiliary_artifacts,
    freetoken_model_profile,
    llama_cpp_model_profiles,
    strata_model_profile,
    strata_model_profiles,
    llama_cpp_model_profile,
    llama_cpp_reasoning_effort_metadata,
    llama_cpp_runtime_distribution,
    managed_local_runtime_for_model,
    normalize_openai_compatible_base_url,
    openai_compatible_local_base_url,
)
from ..router_helpers import cookie_auth_dependency
from .payloads import OllamaModelPayload, OllamaPullPayload
from ...memory.database import get_database_manager
from ...memory.models import (
    KnowledgeField,
    KnowledgeFieldValue,
    KnowledgeNode,
    KnowledgeNodeSupertag,
    KnowledgeSupertag,
    DocsLibrary,
)

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


from ..broadcast_scope import broadcast_llm_state_change


def _attach_server_timing(
    response: JSONResponse,
    name: str,
    started_at: float,
) -> JSONResponse:
    """Attach a stable, proxy-safe Server-Timing diagnostic to *response*.

    Metadata routes are frequently called by both the browser and the BFF.  A
    response header keeps the diagnostic additive (the JSON contracts remain
    unchanged) and survives proxy forwarding without exposing configuration or
    request details.
    """

    duration_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)
    response.headers["Server-Timing"] = f"{name};dur={duration_ms:.1f}"
    return response


def _llama_cpp_model_root_payload(config: Any, *, success: bool | None = None) -> dict[str, Any]:
    """Return the canonical llama.cpp model-root settings envelope."""

    resolved = resolve_llama_cpp_model_root(config)
    getter = getattr(config, "get", None)
    raw = (
        config.get("openai_compatible_local", {}).get("llama_cpp", {})
        if isinstance(config, dict)
        else getter("openai_compatible_local.llama_cpp", {})
        if callable(getter)
        else {}
    )
    raw = raw if isinstance(raw, dict) else {}
    override = canonicalize_llama_cpp_model_root_override(
        raw.get("model_root"),
        create=False,
    )
    payload: dict[str, Any] = {
        "model_root": str(resolved.path),
        "model_root_default": str(resolved.default),
        "model_root_override": str(override or ""),
        "model_root_source": resolved.source,
    }
    if success is not None:
        payload["success"] = bool(success)
    return payload


class _ConfigOverlay:
    """Read-only config view with staged dotted-key overrides."""

    def __init__(self, base: Any, changes: dict[str, Any]):
        self._base = base
        self._changes = changes

    def get(self, key: str, default: Any = None) -> Any:
        parts = key.split(".")
        value = _MISSING_CONFIG_VALUE
        staged_ancestor_found = False

        # A staged parent mapping must also be visible through dotted reads.
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

        # Conversely, staged dotted children must be merged into parent-map
        # reads used by the local OpenAI-compatible client factory.
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


_MISSING_CONFIG_VALUE = object()


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


def _persist_config_changes(
    config: Any,
    changes: dict[str, Any],
) -> None:
    """Persist staged config only after the replacement client is ready.

    Production ``Config`` exposes its complete mapping.  Persisting that
    candidate in one DB transaction prevents a partially-written engine or
    mode selection.  Lightweight test configs retain the existing dotted-key
    persistence contract.
    """

    if not changes:
        return

    live_mapping = getattr(config, "config", None)
    if isinstance(live_mapping, dict):
        from ...app_config_store import update_app_config_keys_sync

        if not update_app_config_keys_sync(changes):
            raise RuntimeError("Failed to persist LLM configuration")
        for key, value in changes.items():
            _set_dotted_mapping_value(live_mapping, key, copy.deepcopy(value))
        return

    if isinstance(config, dict):
        # Lightweight route/test configurations do not expose Config.set or a
        # persistence backend.  Keep their in-memory dotted mapping coherent;
        # production Config instances take the DB-backed branch above.
        for key, value in changes.items():
            _set_dotted_mapping_value(config, key, copy.deepcopy(value))
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


def _restore_llm_client(server: Any, old_client: Any) -> None:
    """Best-effort restoration even if a server lifecycle hook raises."""

    try:
        server.set_llm_client(old_client)
    except Exception:
        logger.exception("Failed to run hooks while restoring previous LLM client")
        server._llm_client = old_client


_LLM_RUNTIME_CHANGE_LOCK = threading.Lock()


@contextmanager
def _llm_runtime_change_gate(*, wait: bool = False):
    # Only setup completion may wait here, on its existing background thread.
    # HTTP/WebSocket admission must never block the event loop or queue a change.
    if not _LLM_RUNTIME_CHANGE_LOCK.acquire(blocking=wait):
        raise HTTPException(status_code=409, detail="LLM runtime configuration is busy; retry after the current change completes")
    try:
        yield
    finally:
        _LLM_RUNTIME_CHANGE_LOCK.release()


def _guard_llm_runtime_change(handler):
    @wraps(handler)
    async def guarded(*args, **kwargs):
        # FastAPI has already resolved cookie/admin dependencies. Buffer HTTP
        # input before admission so a slow body cannot monopolize the gate.
        request = kwargs.get("request") or (args[0] if args else None)
        if isinstance(request, Request):
            await request.body()
        with _llm_runtime_change_gate():
            return await _await_llm_runtime_change(
                asyncio.create_task(handler(*args, **kwargs))
            )

    return guarded


def _commit_strata_runtime_change(
    server: Any,
    *,
    model: str,
    changes: dict[str, Any],
    require_prepared: bool,
) -> None:
    # Setup completion already runs in a worker. Wait rather than fail a
    # completed setup if another runtime mutation is briefly in progress.
    with _llm_runtime_change_gate(wait=True):
        _apply_strata_runtime_change(
            server,
            model=model,
            changes=changes,
            require_prepared=require_prepared,
        )


async def _run_llm_runtime_change_async(
    change: Callable[..., Any], *args, **kwargs
) -> Any:
    return await _await_llm_runtime_change(
        asyncio.create_task(asyncio.to_thread(change, *args, **kwargs))
    )


async def _await_llm_runtime_change(worker: asyncio.Task) -> Any:
    # Cancelling an await cannot stop a synchronous startup or DB transaction.
    # Retain the worker until commit/rollback completes, then propagate request
    # cancellation. This also keeps a cancelled request from abandoning errors.
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as exc:
            if worker.cancelled():
                raise
            cancelled = cancelled or exc
        except Exception:
            if cancelled is None:
                raise
            logger.exception("LLM runtime change failed after request cancellation")
            break
    if cancelled is not None:
        raise cancelled
    return result


def _apply_strata_runtime_change(
    server: Any,
    *,
    model: str,
    changes: dict[str, Any],
    require_prepared: bool,
) -> None:
    """Apply Strata settings transactionally around setup/client/process state.

    Strata setup may take hours and runs in a worker thread.  Keep requested
    settings in a staged overlay until the official setup has completed and an
    active same-model client can be rebound.  If process/client/persistence
    fails, restore the old client and the old owned endpoint before surfacing
    the error.
    """

    from src.llm.manager import create_llm_client
    from src.service_manager import (
        ensure_openai_compatible_local_server,
        stop_strata_server,
    )
    from src.services.strata_runtime import (
        strata_base_url,
        strata_prepared_config,
        strata_settings,
    )

    current_provider = str(server.config.get("llm_provider", "") or "").strip().lower()
    current_model = str(
        server.config.get("llm_model", "")
        or server.config.get("openai_compatible_local.model", "")
        or ""
    ).strip()
    active = current_provider == "openai_compatible_local" and current_model == model
    staged = _ConfigOverlay(server.config, changes)
    old_settings = strata_settings(server.config, model=model)
    new_settings = strata_settings(staged, model=model)
    if require_prepared and new_settings["auto_start"]:
        strata_prepared_config(new_settings)

    old_auto_start = bool(old_settings["auto_start"])
    new_auto_start = bool(new_settings["auto_start"])
    old_client = getattr(server, "_llm_client", None)
    old_runtime_mode = getattr(server, "_current_llm_mode", "")
    new_process_started = False
    old_process_stopped = False
    new_client = None

    try:
        if active and old_auto_start and not new_auto_start:
            old_process_stopped = bool(stop_strata_server(server.config, model=model))
        elif active and new_auto_start:
            # A successful setup/configure must make the selected config live;
            # force_restart is scoped to Strata by service_manager and drains
            # only the same-model endpoint.
            new_process_started = bool(
                ensure_openai_compatible_local_server(
                    staged,
                    model=model,
                    raise_on_launch_error=True,
                    force_restart=True,
                )
            )

        if active:
            new_client = create_llm_client(staged)
            next_mode_state = build_llm_mode_state(staged, client=new_client)
            next_mode = str(next_mode_state.get("mode") or "").strip()
            if (
                next_mode_state.get("kind") == "response_mode"
                and hasattr(new_client, "set_llm_mode")
            ):
                new_client.set_llm_mode(next_mode)
            server.set_llm_client(new_client)

        _persist_config_changes(server.config, changes)
        if active and new_client is not None:
            server._current_llm_mode = next_mode
    except Exception:
        if active:
            _restore_llm_client(server, old_client)
            server._current_llm_mode = old_runtime_mode
            try:
                if new_auto_start and (new_process_started or old_process_stopped):
                    stop_strata_server(staged, model=model)
            except Exception:
                logger.exception("Failed to stop replacement Strata process during rollback")
            if old_auto_start:
                try:
                    ensure_openai_compatible_local_server(
                        server.config,
                        model=model,
                        raise_on_launch_error=False,
                        force_restart=False,
                    )
                except Exception:
                    logger.exception("Failed to restore previous Strata process during rollback")
        raise


def _extract_json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if not value:
        raise ValueError("empty LLM response")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", value)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response is not a JSON object")
    return parsed


def _normalize_docs_ai_result(command: str, parsed: dict[str, Any]) -> dict[str, Any]:
    mode = str(parsed.get("mode") or "").strip()
    if command == "rewrite":
        replacement = str(parsed.get("replacement") or parsed.get("title") or "").strip()
        return {
            "mode": "replace_title",
            "replacement": replacement[:500],
            "summary": str(parsed.get("summary") or "AI rewrite proposal").strip()[:500],
        }
    if command == "fill_fields":
        fields = parsed.get("fields")
        normalized_fields = []
        if isinstance(fields, list):
            for item in fields[:20]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("field") or "").strip()
                value = str(item.get("value") or "").strip()
                if name:
                    normalized_fields.append({"name": name[:120], "value": value[:1000]})
        return {
            "mode": "field_suggestions",
            "fields": normalized_fields,
            "summary": str(parsed.get("summary") or "AI field suggestions").strip()[:500],
        }
    lines = parsed.get("lines")
    if not isinstance(lines, list):
        lines = parsed.get("children")
    normalized_lines = [
        str(item).strip()[:500]
        for item in (lines if isinstance(lines, list) else [])
        if str(item).strip()
    ][:12]
    if command == "extract_tasks":
        normalized_lines = [
            line if "#Task" in line or "#タスク" in line else f"{line} #Task"
            for line in normalized_lines
        ]
    return {
        "mode": mode if mode == "insert_children" else "insert_children",
        "lines": normalized_lines,
        "summary": str(parsed.get("summary") or "AI child node proposal").strip()[:500],
    }


async def _run_docs_ai_completion(llm_client: Any, prompt: str) -> str:
    async_generate = getattr(llm_client, "generate_response_async", None)
    if callable(async_generate):
        return str(await async_generate(prompt))
    async_generic = getattr(llm_client, "generate_async", None)
    if callable(async_generic):
        return str(await async_generic(prompt))
    sync_generate = getattr(llm_client, "generate_response", None)
    if callable(sync_generate):
        return str(await asyncio.to_thread(lambda: sync_generate(prompt, stream=False)))
    sync_generic = getattr(llm_client, "generate", None)
    if callable(sync_generic):
        return str(await asyncio.to_thread(lambda: sync_generic(prompt)))
    raise RuntimeError("Configured LLM client does not support text generation")


async def _build_docs_ai_context(node_id: str | None, user_id: str | None = None) -> str:
    if not node_id:
        return ""
    try:
        parsed_id = uuid.UUID(str(node_id))
    except ValueError:
        return ""
    manager = get_database_manager()
    session = await manager.get_session()
    try:
        node = await session.get(KnowledgeNode, parsed_id)
        if node is None:
            return ""
        library = await session.get(DocsLibrary, node.docs_library_id)
        if user_id:
            from ...services.docs_acl import can_read_node, docs_readable_node_predicate

            try:
                if not await can_read_node(session, node, uuid.UUID(str(user_id))):
                    return ""
            except (TypeError, ValueError):
                return ""
        visibility = None
        if user_id:
            visibility = docs_readable_node_predicate(
                KnowledgeNode,
                docs_library_id=node.docs_library_id,
                user_id=uuid.UUID(str(user_id)),
                library_owner_id=getattr(library, "owner_user_id", None),
            )
        parent = await session.get(KnowledgeNode, node.parent_id) if node.parent_id else None
        siblings_result = await session.execute(
            select(KnowledgeNode)
            .where(
                KnowledgeNode.docs_library_id == node.docs_library_id,
                KnowledgeNode.parent_id == node.parent_id,
                KnowledgeNode.archived_at.is_(None),
                visibility if visibility is not None else True,
            )
            .order_by(KnowledgeNode.sort_order, KnowledgeNode.created_at)
            .limit(16)
        )
        siblings = list(siblings_result.scalars().all())
        tags_result = await session.execute(
            select(KnowledgeSupertag)
            .join(KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeSupertag.id)
            .where(KnowledgeNodeSupertag.node_id == node.id)
            .order_by(KnowledgeSupertag.name)
        )
        tags = list(tags_result.scalars().all())
        fields_result = await session.execute(
            select(KnowledgeField, KnowledgeFieldValue)
            .join(KnowledgeNodeSupertag, KnowledgeNodeSupertag.supertag_id == KnowledgeField.supertag_id)
            .outerjoin(
                KnowledgeFieldValue,
                (KnowledgeFieldValue.node_id == node.id)
                & (KnowledgeFieldValue.field_id == KnowledgeField.id),
            )
            .where(KnowledgeNodeSupertag.node_id == node.id)
            .order_by(KnowledgeField.sort_order, KnowledgeField.name)
        )
        field_lines = []
        for field, value in fields_result.all():
            raw_value = ""
            if value is not None:
                raw_value = (
                    value.value_text
                    or (str(value.value_number) if value.value_number is not None else "")
                    or (value.value_datetime.isoformat() if value.value_datetime else "")
                    or (str(value.target_node_id) if value.target_node_id else "")
                    or (json.dumps(value.value_json, ensure_ascii=False) if value.value_json else "")
                )
            field_lines.append(f"- {field.name} ({field.system_key or field.field_type}): {raw_value or '(empty)'}")
        instructions = "\n".join(
            str(tag.ai_instructions or "").strip()
            for tag in tags
            if str(tag.ai_instructions or "").strip()
        )
        return "\n".join(
            [
                "Docs context:",
                f"Current node: {node.title or ''}",
                f"Body: {(node.body_text or '')[:1000]}",
                f"Parent: {parent.title if parent else '(root)'}",
                "Sibling outline:",
                *[f"- {item.title or ''}" for item in siblings],
                "Tags: " + ", ".join(f"#{tag.name}" for tag in tags),
                "Fields:",
                *field_lines,
                "AI instructions:",
                instructions or "(none)",
            ]
        )
    finally:
        await session.close()


def _should_stop_previous_openai_compatible_local_server(
    previous_provider: str,
    previous_model: str,
    next_provider: str,
    next_model: str,
    *,
    previous_config: Any | None = None,
    next_config: Any | None = None,
    previous_ownership: ProviderRuntimeOwnership | None = None,
    next_ownership: ProviderRuntimeOwnership | None = None,
) -> bool:
    """Return whether switching routes should stop an owned local process.

    The four positional arguments are kept for callers that predate the
    ownership contract.  In that compatibility form a non-``local-model``
    local selection is treated as managed, matching the historical route
    behaviour.  Engine switching itself always passes resolved ownership
    metadata, making ``managed_runtime`` the sole lifecycle gate.
    """

    previous_provider_id = str(previous_provider or "").strip().lower()
    next_provider_id = str(next_provider or "").strip().lower()
    if previous_provider_id != "openai_compatible_local":
        return False

    # A caller may provide configs instead of pre-resolved ownership.  Keep
    # this resolution here so the helper is safe to use from future routes as
    # well as the engine-switch route below.
    if previous_ownership is None and previous_config is not None:
        previous_ownership = provider_runtime_ownership(
            previous_provider_id,
            previous_config,
            model=previous_model,
        )
    if next_ownership is None and next_config is not None:
        next_ownership = provider_runtime_ownership(
            next_provider_id,
            next_config,
            model=next_model,
        )

    # Preserve the old four-argument contract when no ownership context is
    # supplied.  The route never takes this branch.
    previous_managed = (
        True if previous_ownership is None else previous_ownership.managed_runtime
    )
    if next_provider_id != "openai_compatible_local":
        return bool(previous_managed)

    next_managed = (
        True if next_ownership is None else next_ownership.managed_runtime
    )
    if not previous_managed:
        # An operator-owned endpoint must never be stopped by model/provider
        # selection, including when the next selection is AoiTalk-managed.
        return False
    if not next_managed:
        # Leaving an AoiTalk-owned process for an operator-owned endpoint is
        # the one ownership transition that still requires stopping the old
        # process.  The new endpoint itself is never touched.
        return True
    return str(previous_model or "").strip() != str(next_model or "").strip()


def _selection_runtime_ownership(
    config: Any,
    provider: str,
    model: str,
    *,
    llama_cpp_settings: dict[str, Any] | None = None,
    freetoken_settings: dict[str, Any] | None = None,
) -> ProviderRuntimeOwnership:
    """Resolve ownership against a requested (possibly staged) selection.

    ``provider_runtime_ownership`` intentionally reads persisted config.  A
    request may, however, include a new ``auto_start`` value before that value
    is persisted.  Overlay only the route's requested selection/settings so
    the ownership result reflects the effective candidate without mutating
    the live config.
    """

    provider_id = str(provider or "").strip().lower()
    if provider_id != "openai_compatible_local":
        return provider_runtime_ownership(provider_id, config, model=model)
    changes: dict[str, Any] = {
        "openai_compatible_local.model": model,
    }
    for key, value in (llama_cpp_settings or {}).items():
        changes[f"openai_compatible_local.llama_cpp.{key}"] = value
    for key, value in (freetoken_settings or {}).items():
        changes[
            f"openai_compatible_local.freetoken.{key}"
        ] = value
    candidate = _ConfigOverlay(config, changes) if changes else config
    return provider_runtime_ownership(provider_id, candidate, model=model)


_LLAMA_CPP_SETTING_KEYS = {
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
    # Managed MTP controls are persisted alongside the existing llama.cpp
    # settings.  The runtime resolves whether the requested profile can
    # actually use them; callers must not encode MTP flags in extra_args.
    "mtp_enabled",
}

_FREETOKEN_SETTING_KEYS = {
    "host",
    "port",
    "auto_start",
    "readiness_timeout",
    "extra_args",
}


def _normalize_freetoken_request_settings(
    body: dict[str, Any],
) -> dict[str, Any]:
    """Validate the writable FreeToken engine-switch settings."""

    raw = body.get("freetoken")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400,
            detail="freetoken はオブジェクトで指定してください",
        )

    unknown = sorted(set(raw) - _FREETOKEN_SETTING_KEYS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                "FreeTokenの未対応設定: "
                + ", ".join(str(item) for item in unknown)
            ),
        )

    result: dict[str, Any] = {}
    if "host" in raw:
        host = raw["host"]
        if not isinstance(host, str) or not host.strip():
            raise HTTPException(
                status_code=400,
                detail="freetoken.host は空でない文字列で指定してください",
            )
        result["host"] = host.strip()

    if "port" in raw:
        value = raw["port"]
        if isinstance(value, bool):
            raise HTTPException(
                status_code=400,
                detail="freetoken.port は整数で指定してください",
            )
        try:
            port = int(value)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="freetoken.port は整数で指定してください",
            ) from None
        if not 1 <= port <= 65535:
            raise HTTPException(
                status_code=400,
                detail="freetoken.port の値が範囲外です",
            )
        result["port"] = port

    if "auto_start" in raw:
        value = raw["auto_start"]
        if not isinstance(value, (bool, int, str)):
            raise HTTPException(
                status_code=400,
                detail="freetoken.auto_start は真偽値で指定してください",
            )
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized not in {
                "1",
                "0",
                "true",
                "false",
                "yes",
                "no",
                "on",
                "off",
            }:
                raise HTTPException(
                    status_code=400,
                    detail="freetoken.auto_start は真偽値で指定してください",
                )
            value = normalized in {"1", "true", "yes", "on"}
        result["auto_start"] = bool(value)

    if "readiness_timeout" in raw:
        value = raw["readiness_timeout"]
        if isinstance(value, bool):
            raise HTTPException(
                status_code=400,
                detail="freetoken.readiness_timeout は正数で指定してください",
            )
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="freetoken.readiness_timeout は正数で指定してください",
            ) from None
        if timeout <= 0:
            raise HTTPException(
                status_code=400,
                detail="freetoken.readiness_timeout は正数で指定してください",
            )
        result["readiness_timeout"] = timeout

    if "extra_args" in raw:
        value = raw["extra_args"]
        if isinstance(value, str):
            normalized_extra_args: Any = value
        elif isinstance(value, list) and all(
            isinstance(item, (str, int, float))
            for item in value
        ):
            normalized_extra_args = [
                str(item)
                for item in value
                if str(item).strip()
            ]
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    "freetoken.extra_args は文字列または配列で指定してください"
                ),
            )
        try:
            from src.service_manager._local_llm_servers import (
                _validate_freetoken_extra_args,
            )

            _validate_freetoken_extra_args(normalized_extra_args)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc
        result["extra_args"] = normalized_extra_args

    return result


def _normalize_llama_cpp_request_settings(body: dict[str, Any]) -> dict[str, Any]:
    """Validate the nested llama.cpp contract without accepting shell text."""

    raw = body.get("llama_cpp")
    if raw is None:
        raw = body.get("runtime_settings")
    if raw is None and isinstance(body.get("runtime"), dict):
        raw = body.get("runtime")
    if raw is None:
        # Accepting these top-level keys keeps older settings clients usable,
        # while the persisted shape remains nested under llama_cpp.
        raw = {
            key: body[key]
            for key in _LLAMA_CPP_SETTING_KEYS
            if key in body
        }
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="llama_cpp はオブジェクトで指定してください")

    # Catalog responses include descriptive runtime metadata.  Ignore those
    # read-only keys when a UI posts the settings object back verbatim.
    readonly_keys = {
        "runtime",
        "server_profile",
        "base_url",
        "muse_model_filename",
        "model_filename",
        "official_filename",
        "filename",
        "served_alias",
        "quantization",
        "jinja_required",
        "muse_minimum_llama_cpp_build",
        "minimum_llama_cpp_build",
        "profile_id",
        "served_alias",
        "required_args",
        "default_args",
        "native_context_length",
        "native_context_size",
        "gguf_filename",
        "gguf_filenames",
        "gguf_shard_count",
        "gguf_repository_subdir",
        "default_gpu_layers",
        "required_llama_cpp_commit",
        "source_url",
        "reasoning_tools_minimum_llama_cpp_build",
        "supports_reasoning",
        "supports_tools",
        "supports_media",
        "quantization",
        "capabilities",
        "source_repository",
        "huggingface_repository",
        "huggingface_repo",
        "runtime_profile",
        "alias_locked",
        # Canonical MTP/profile metadata is descriptive and is accepted when
        # a catalog settings object is posted back verbatim.  Only the two
        # inputs above are writable through this route.
        "mtp",
        "mtp_supported",
        "mtp_available",
        "mtp_status",
        "mtp_reason",
        "mtp_artifact_path",
        "mtp_model_path",
        "mtp_resolved_model_path",
        "mtp_variant_model_path",
        "mtp_mode",
        "mtp_default_enabled",
        "mtp_artifact_filename",
        "mtp_compatibility",
        "mtp_ui_notice",
        # Flattened aliases emitted by older catalog clients; nested MTP
        # metadata is descriptive and never writable either way.
        "mtp_minimum_llama_cpp_build",
        "mtp_required_llama_cpp_commit",
        "mtp_embedded_variant",
    }
    unknown = sorted(
        set(raw) - _LLAMA_CPP_SETTING_KEYS - {"readiness_timeout_seconds"} - readonly_keys
    )
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"llama_cpp の未対応設定: {', '.join(str(item) for item in unknown)}",
        )

    result: dict[str, Any] = {}
    for key in ("executable", "model_path", "model_root", "model_alias", "host"):
        if key in raw:
            value = raw[key]
            if key == "model_root" and value is None:
                result[key] = ""
                continue
            if not isinstance(value, str):
                raise HTTPException(status_code=400, detail=f"llama_cpp.{key} は文字列で指定してください")
            if key == "model_root":
                try:
                    canonical_root = canonicalize_llama_cpp_model_root_override(
                        value,
                        create=True,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                result[key] = str(canonical_root or "")
            else:
                result[key] = value.strip()
    for key in ("port", "context_size"):
        if key in raw:
            try:
                value = int(raw[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"llama_cpp.{key} は整数で指定してください")
            if value <= 0 or (key == "port" and value > 65535):
                raise HTTPException(status_code=400, detail=f"llama_cpp.{key} の値が範囲外です")
            result[key] = value
    if "gpu_layers" in raw:
        value = raw["gpu_layers"]
        if isinstance(value, str) and value.strip().casefold() == "auto":
            result["gpu_layers"] = "auto"
        else:
            try:
                result["gpu_layers"] = int(value)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail="llama_cpp.gpu_layers は整数またはautoで指定してください",
                )
    if "extra_args" in raw:
        value = raw["extra_args"]
        if isinstance(value, str):
            # The runtime accepts a string for environment compatibility; API
            # callers should prefer an argv array to avoid shell ambiguity.
            result["extra_args"] = value
        elif isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
            result["extra_args"] = [str(item) for item in value if str(item).strip()]
        else:
            raise HTTPException(status_code=400, detail="llama_cpp.extra_args は文字列または配列で指定してください")
        try:
            from src.service_manager import _validate_llama_cpp_extra_args

            _validate_llama_cpp_extra_args(result["extra_args"])
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "auto_start" in raw:
        value = raw["auto_start"]
        if not isinstance(value, (bool, int, str)):
            raise HTTPException(status_code=400, detail="llama_cpp.auto_start は真偽値で指定してください")
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
                raise HTTPException(status_code=400, detail="llama_cpp.auto_start は真偽値で指定してください")
            value = normalized in {"1", "true", "yes", "on"}
        result["auto_start"] = bool(value)
    if "mtp_enabled" in raw:
        value = raw["mtp_enabled"]
        if not isinstance(value, (bool, int, str)):
            raise HTTPException(status_code=400, detail="llama_cpp.mtp_enabled は真偽値で指定してください")
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
                raise HTTPException(status_code=400, detail="llama_cpp.mtp_enabled は真偽値で指定してください")
            value = normalized in {"1", "true", "yes", "on"}
        result["mtp_enabled"] = bool(value)
    timeout_key = "readiness_timeout" if "readiness_timeout" in raw else "readiness_timeout_seconds"
    if timeout_key in raw:
        try:
            timeout = float(raw[timeout_key])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="llama_cpp.readiness_timeout は正数で指定してください")
        if timeout <= 0:
            raise HTTPException(status_code=400, detail="llama_cpp.readiness_timeout は正数で指定してください")
        result["readiness_timeout"] = timeout
    return result


def _llama_cpp_runtime_settings_changed(
    config: Any,
    *,
    model: str,
    settings: dict[str, Any],
) -> bool:
    """Detect same-model runtime edits before mutating the persisted config."""

    if not settings:
        return False
    from src.service_manager import _config_get

    previous_provider = str(_config_get(config, "llm_provider", "") or "").strip().casefold()
    previous_model = str(
        _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()
    if previous_provider != "openai_compatible_local" or previous_model != model:
        return False
    from src.llm.openai_compatible_local_profiles import (
        normalize_openai_compatible_base_url,
        openai_compatible_local_base_url,
    )
    from src.service_manager import _llama_cpp_base_url, _llama_cpp_settings

    old_effective = _llama_cpp_settings(config, model=previous_model)
    new_effective = _llama_cpp_settings(
        config,
        model=model,
        overrides=settings,
    )
    if old_effective != new_effective:
        return True
    old_base_url = openai_compatible_local_base_url(config, model=previous_model)
    new_base_url = _llama_cpp_base_url(config, model=model, overrides=settings)
    return normalize_openai_compatible_base_url(old_base_url) != normalize_openai_compatible_base_url(new_base_url)


def _freetoken_runtime_settings_changed(
    config: Any,
    *,
    model: str,
    settings: dict[str, Any],
) -> bool:
    """Detect same-model FreeToken runtime edits without mutating config."""

    if not settings:
        return False
    from src.service_manager import _config_get

    previous_provider = str(
        _config_get(config, "llm_provider", "") or ""
    ).strip().casefold()
    previous_model = str(
        _config_get(config, "openai_compatible_local.model", "")
        or _config_get(config, "llm_model", "")
        or ""
    ).strip()
    if (
        previous_provider != "openai_compatible_local"
        or previous_model != model
    ):
        return False

    from src.service_manager import _freetoken_settings

    previous_settings = _freetoken_settings(
        config,
        model=model,
    )
    return any(
        previous_settings.get(key) != value
        for key, value in settings.items()
    )


def _local_runtime_public_reason(state: dict[str, Any]) -> str | None:
    """Return a path/secret-free explanation for one manager state."""

    status = str(state.get("status") or "").strip().lower()
    reasons = {
        "external": "This model is not managed by AoiTalk.",
        "installer_required": (
            "This runtime requires an interactive/manual installer on this platform."
        ),
        "unsupported": "This managed runtime is not supported on this platform.",
        "runtime_missing": (
            "The managed runtime executable is not installed or configured."
        ),
        "manual_model_required": (
            "This managed profile requires a manually provided model artifact."
        ),
        "model_missing": "The managed model artifact is not prepared.",
        "failed": (
            "Managed local runtime preparation failed. "
            "Check server logs for details."
        ),
        "cancelling": "Managed local runtime preparation is being cancelled.",
        "cancelled": "Managed local runtime preparation was cancelled.",
        "not_found": "Local runtime prepare task not found.",
    }
    return reasons.get(status)


def _probe_llama_cpp_endpoint(endpoint: str) -> str:
    """Classify a local endpoint without sending an HTTP request.

    ``_owned_llama_cpp_process_at`` only knows about tracked workers.  A
    listener left by another process must therefore not be treated as an idle
    managed worker.  Keep the probe bounded and preserve an explicit
    ``unknown`` result: only a real connection refusal proves that the port is
    closed and authorizes the waiting projection.
    """

    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "unknown"
        port = parsed.port
        if port is None:
            return "unknown"
        with socket.create_connection(
            (parsed.hostname, port),
            timeout=0.35,
        ):
            return "open"
    except ConnectionRefusedError:
        return "closed"
    except (PermissionError, TimeoutError, OSError, ValueError):
        return "unknown"


def _local_runtime_api_payload(
    state: dict[str, Any],
    config: Any = None,
) -> dict[str, Any]:
    """Project manager state without exposing local paths or raw exceptions."""

    runtime = str(state.get("runtime") or "").strip() or None
    phase = str(state.get("phase") or "unknown").strip() or "unknown"
    status = str(state.get("status") or "unknown").strip() or "unknown"
    task_id = str(state.get("task_id") or "").strip() or None
    model = str(state.get("model") or "").strip()
    runtime_installed = bool(state.get("runtime_installed"))
    model_installed = bool(state.get("model_installed"))
    prepared = bool(state.get("prepared"))
    done = bool(state.get("done"))
    prepare_supported = bool(state.get("prepare_supported", False))
    reason = _local_runtime_public_reason(state)
    managed = runtime is not None
    platform_supported = bool(managed and status not in {"installer_required", "unsupported"})
    runtime_version = (
        str(state["runtime_version"]).strip()
        if isinstance(state.get("runtime_version"), str)
        and str(state["runtime_version"]).strip()
        else None
    )
    runtime_build = (
        state.get("runtime_build")
        if isinstance(state.get("runtime_build"), int)
        and not isinstance(state.get("runtime_build"), bool)
        else None
    )
    runtime_install_source = str(state.get("runtime_install_source") or "").strip() or None
    runtime_root_source = str(state.get("runtime_root_source") or "").strip() or None
    runtime_release = str(state.get("runtime_release") or "").strip() or None
    runtime_asset = str(state.get("runtime_asset") or "").strip() or None

    runtime_install_state = (
        state.get("runtime_install")
        if isinstance(state.get("runtime_install"), dict)
        else {}
    )
    model_artifact_state = (
        state.get("model_artifact")
        if isinstance(state.get("model_artifact"), dict)
        else {}
    )
    server_state = (
        state.get("server")
        if isinstance(state.get("server"), dict)
        else {}
    )
    state_actions = (
        state.get("actions")
        if isinstance(state.get("actions"), dict)
        else {}
    )

    profile = (
        llama_cpp_model_profile(model)
        if runtime == "llama_cpp"
        else freetoken_model_profile(model)
        if runtime == "freetoken"
        else strata_model_profile(model)
        if runtime == "strata"
        else None
    )
    auxiliary_contract = []
    if runtime == "llama_cpp" and profile is not None:
        try:
            auxiliary_contract = llama_cpp_auxiliary_artifacts(profile=profile)
        except ValueError:
            auxiliary_contract = []
    runtime_distribution = (
        llama_cpp_runtime_distribution(profile=profile)
        if runtime == "llama_cpp"
        else None
    )
    auxiliary_projection = [
        {
            **artifact,
            "status": "installed" if model_installed else "missing",
            "installed": model_installed,
        }
        for artifact in auxiliary_contract
    ]
    profile_downloadable = bool(
        profile
        and profile.get("source_repository")
        and (runtime in {"freetoken", "strata"} or profile.get("gguf_filename"))
    )
    explicit_runtime_downloadable = runtime_install_state.get(
        "downloadable",
        state.get("runtime_downloadable"),
    )
    runtime_downloadable = (
        explicit_runtime_downloadable
        if isinstance(explicit_runtime_downloadable, bool)
        else bool(runtime == "llama_cpp" and platform_supported)
    )
    explicit_model_downloadable = model_artifact_state.get(
        "downloadable",
        state.get("model_artifact_downloadable", state.get("model_downloadable")),
    )
    model_downloadable = (
        explicit_model_downloadable
        if isinstance(explicit_model_downloadable, bool)
        else profile_downloadable
    )

    if runtime is None:
        runtime_install_status = "not_applicable"
    elif runtime_installed:
        runtime_install_status = "installed"
    elif status in {"installer_required", "unsupported"}:
        runtime_install_status = status
    else:
        runtime_install_status = "missing"

    if runtime is None:
        model_artifact_status = "not_applicable"
    elif model_installed:
        model_artifact_status = "installed"
    elif not done and phase in {"queued", "model", "verify"}:
        model_artifact_status = "preparing"
    elif status == "failed":
        model_artifact_status = "failed"
    elif status == "manual_model_required":
        model_artifact_status = "manual_required"
    else:
        model_artifact_status = "missing"

    running_value = server_state.get("running", state.get("server_running"))
    ready_value = server_state.get("ready", state.get("server_ready"))
    server_running = running_value if isinstance(running_value, bool) else False
    server_ready = ready_value if isinstance(ready_value, bool) else None
    endpoint_offline = server_state.get("endpoint_offline") is True
    server_base_url = None
    if managed and model:
        try:
            server_base_url = openai_compatible_local_base_url(
                config,
                model=model,
            )
        except Exception:
            server_base_url = None

    def _explicit_action(name: str) -> bool:
        for value in (
            state_actions.get(name),
            state.get(f"supports_{name}"),
            state.get(f"{name}_supported"),
        ):
            if isinstance(value, bool):
                return value
        return False

    strata_public = None
    if runtime == "strata":
        from src.services.strata_runtime import SETUP_NOTICE

        settings = _strata_static_catalog_settings(config, model)
        strata_public = {key: settings[key] for key in ("context_size", "vision", "host", "port", "auto_start")}
        strata_public.update({"operator_only": settings["operator_only"], "setup_notice": SETUP_NOTICE, "source_revision": profile["source_revision"], "source_url": profile["source_url"]})

    server_public: dict[str, Any] = {
        "status": str(
            server_state.get("status")
            or ("not_checked" if managed else "external")
        ),
        "running": server_running,
        "ready": server_ready if managed else False,
        "base_url": server_base_url,
    }
    if runtime == "strata":
        startup_progress = (
            server_state.get("startup_progress")
            if isinstance(server_state.get("startup_progress"), dict)
            else None
        )
        if startup_progress is not None:
            phase = startup_progress.get("phase")
            elapsed_s = startup_progress.get("elapsed_s")
            if phase not in {"loading", "reading_weights", "allocating_cache", "initializing"}:
                startup_progress = None
            elif not isinstance(elapsed_s, (int, float)) or isinstance(elapsed_s, bool) or elapsed_s < 0:
                startup_progress = None
            else:
                startup_progress = {"phase": phase, "elapsed_s": elapsed_s}
        server_public.update(
            {
                "context_size": server_state.get("context_size"),
                "vision": server_state.get("vision"),
                "startup_progress": startup_progress,
                # Prepared + auto_start + stopped is the normal idle state:
                # the next chat turn starts Strata (lazy start).
                "auto_start_on_send": bool(
                    strata_public
                    and strata_public["auto_start"]
                    and not strata_public["operator_only"]
                    and prepared
                    and not server_running
                    and server_public["status"] == "offline"
                    and endpoint_offline
                ),
            }
        )
    elif runtime == "llama_cpp":
        # The status route marks a prepared, stopped managed llama.cpp worker
        # as ``waiting`` only when its auto-start setting has already been
        # validated. Preserve that proof for the UI without treating any
        # other offline, failed, live, or identity-mismatched state as lazy
        # loadable.
        server_public["auto_start_on_send"] = bool(
            managed
            and prepared
            and server_public["status"] == "waiting"
            and not server_running
            and server_ready is False
            and endpoint_offline
        )

    return {
        "task_id": task_id,
        "strata": strata_public,
        "model": model,
        "runtime": runtime,
        "runtime_distribution": runtime_distribution,
        "managed": managed,
        "platform_supported": platform_supported,
        "phase": phase,
        "status": status,
        "completed": int(state.get("completed") or 0),
        "total": int(state.get("total") or 0),
        "percent": int(state.get("percent") or 0),
        "done": done,
        # Never expose the manager's raw exception text here. Download/runtime
        # failures may contain local filesystem paths or upstream diagnostics.
        "error": reason if state.get("error") else None,
        "runtime_installed": runtime_installed,
        "model_installed": model_installed,
        "prepared": prepared,
        "started_at": (
            state.get("started_at")
            if isinstance(state.get("started_at"), str)
            else None
        ),
        "updated_at": (
            state.get("updated_at")
            if isinstance(state.get("updated_at"), str)
            else None
        ),
        "prepare_supported": prepare_supported,
        "runtime_version": runtime_version,
        "runtime_build": runtime_build,
        "runtime_install_source": runtime_install_source,
        # Keep provenance without exposing the manager's local filesystem
        # root; the raw manager state retains runtime_root for diagnostics.
        "runtime_root_source": runtime_root_source,
        "runtime_release": runtime_release,
        "runtime_asset": runtime_asset,
        "runtime_install": {
            "runtime": runtime,
            **(
                {"distribution": runtime_distribution}
                if runtime_distribution
                else {}
            ),
            "installed": runtime_installed,
            "status": runtime_install_status,
            "installer_required": status == "installer_required",
            "version": runtime_version,
            "build": runtime_build,
            "install_source": runtime_install_source,
            "release": runtime_release,
            "asset": runtime_asset,
            "downloadable": runtime_downloadable,
        },
        "model_artifact": {
            "installed": model_installed,
            "status": model_artifact_status,
            "managed": managed,
            "downloadable": model_downloadable,
            "primary_filename": (
                str(profile.get("gguf_filename") or "")
                if profile and runtime == "llama_cpp"
                else None
            ),
            "auxiliary_artifacts": auxiliary_projection,
        },
        # B2a prepares runtime/model artifacts only. Server launch/readiness is
        # intentionally not inferred from filesystem state.
        "server": server_public,
        "actions": {
            "prepare": bool(
                prepare_supported and done and not prepared
            ),
            "poll": bool(task_id and not done),
            "cancel": bool(
                runtime == "strata"
                and
                task_id
                and not done
                and status in {"queued", "running", "preparing", "cancelling"}
            ),
            "retry": bool(
                prepare_supported
                and done
                and status == "failed"
            ),
            "installer_required": status == "installer_required",
            "start": _explicit_action("start"),
            "stop": _explicit_action("stop"),
            "delete_model": _explicit_action("delete_model"),
        },
        "reason": reason,
    }


def register_llm_routes(app: FastAPI, server: "WebChatServer") -> None:
    """LLM mode / models / engine / Ollama 管理ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    local_runtime_manager = getattr(
        server,
        "_local_llm_runtime_manager",
        None,
    )
    if local_runtime_manager is None:
        # Lightweight route tests and fallback server implementations do not
        # necessarily construct the production WebChatServer.
        from ...services.local_llm_runtime_manager import ManagedLocalRuntimeManager

        local_runtime_manager = ManagedLocalRuntimeManager(server.config)
        server._local_llm_runtime_manager = local_runtime_manager

    # Explicit runtime discovery may fan out to several loopback providers and
    # creates its own bounded worker pool inside the catalog helper. Keep one
    # such operation in flight per FastAPI loop; metadata/static reads do not
    # acquire this gate.
    runtime_discovery_gate = asyncio.Semaphore(1)
    runtime_discovery_queue_timeout = 0.25
    # A normal catalog read is a metadata snapshot, so concurrent settings and
    # runtime-context requests should share one worker.  Shield the worker
    # from request cancellation; otherwise each browser timeout would leave a
    # new filesystem-heavy catalog build behind it.
    normal_catalog_tasks: dict[str, asyncio.Task[Any]] = {}
    # Cloud registry refreshes are independent from local runtime discovery.
    # Coalesce explicit DeepInfra refreshes as well as the TTL-triggered
    # normal read, so several settings tabs cannot fan out identical GETs.
    deepinfra_catalog_tasks: dict[str, asyncio.Task[Any]] = {}
    deepinfra_refresh_retry_until: dict[str, float] = {}
    deepinfra_refresh_errors: dict[str, str] = {}
    model_catalog_cache_write_lock = asyncio.Lock()

    def _normal_catalog_snapshot_key(
        config: Any,
        provider: Optional[str],
    ) -> str:
        """Build a secret-free key for one current catalog snapshot."""

        def value(key: str) -> Any:
            getter = getattr(config, "get", None)
            if callable(getter):
                return getter(key, None)
            if isinstance(config, dict):
                current: Any = config
                for part in key.split("."):
                    if not isinstance(current, dict) or part not in current:
                        return None
                    current = current[part]
                return current
            return None

        snapshot = {
            "provider": str(provider or "").strip().lower(),
            "llm_provider": value("llm_provider"),
            "llm_model": value("llm_model"),
            "local_model": value("openai_compatible_local.model"),
            "local_base_url": value("openai_compatible_local.base_url"),
            "local_server_profile": value("openai_compatible_local.server_profile"),
            "hidden_provider_ids": value("llm_provider_visibility.hidden_provider_ids"),
            "runtime_mode": value("llm_runtime_mode"),
            "config_revision": value("config_revision")
            or value("app_config_revision")
            or value("app_config_schema_version"),
            # The model-list endpoint and connection host affect the effective
            # DeepInfra catalog. Include only non-secret connection metadata;
            # the complete snapshot is hashed before it becomes a task key.
            "deepinfra": {
                "base_url": value("deepinfra.base_url")
                or value("deepinfra_base_url"),
                "models_url": value("deepinfra.models_url"),
                "api_key_configured": bool(
                    value("deepinfra_api_key")
                    or os.environ.get("DEEPINFRA_TOKEN")
                ),
                "base_url_env": os.environ.get("DEEPINFRA_BASE_URL"),
                "models_url_env": os.environ.get("DEEPINFRA_MODELS_URL"),
            },
            "strata": {
                key: value(f"openai_compatible_local.strata.{key}")
                for key in (
                    "source_root",
                    "data_dir",
                    "gguf_dir",
                    "host",
                    "port",
                    "context_size",
                    "vision",
                    "auto_start",
                    "readiness_timeout",
                    "prefill",
                    "vram_reserve_mib",
                    "expert_cache",
                )
            },
            "freetoken": {
                key: value(f"openai_compatible_local.freetoken.{key}")
                for key in ("host", "port", "auto_start")
            },
            # Local profile discovery is read-only but depends on the selected
            # root, executable, and per-profile launch controls. Include only
            # operational fields in the coalescing key; credentials and other
            # secret-bearing settings must never enter it.
            "llama_cpp": {
                key: value(f"openai_compatible_local.llama_cpp.{key}")
                for key in (
                    "model_root",
                    "model_dir",
                    "model_path",
                    "model_alias",
                    "executable",
                    "auto_start",
                    "context_size",
                    "gpu_layers",
                    "extra_args",
                    "readiness_timeout",
                    "mtp_enabled",
                )
            },
            # These environment values are part of the effective local
            # runtime resolver. Keep the allowlist limited to operational
            # paths/toggles; provider credentials are deliberately omitted.
            "environment": {
                name: os.environ.get(name)
                for name in (
                    "LLAMA_CPP_MODEL_ROOT",
                    "AOITALK_LLAMA_CPP_MODEL_ROOT",
                    "AOITALK_REQUIRE_EXTERNAL_LLM_MODEL_ROOT",
                    "LLAMA_CPP_EXECUTABLE",
                    "LLAMA_SERVER_EXE",
                    "LLAMA_CPP_MODEL_PATH",
                    "LLAMA_CPP_MODEL_ALIAS",
                    "LLAMA_CPP_AUTO_START",
                    "STRATA_SOURCE_ROOT",
                    "STRATA_DATA_DIR",
                    "STRATA_GGUF_DIR",
                    "STRATA_AUTO_START",
                    "FREETOKEN_HOST",
                    "FREETOKEN_PORT",
                    "FREETOKEN_AUTO_START",
                )
            },
            "registered_profiles": {
                "llama_cpp": sorted(
                    str(profile.get("id") or "")
                    for profile in llama_cpp_model_profiles()
                    if isinstance(profile, dict) and profile.get("id")
                ),
                "strata": sorted(
                    str(profile.get("id") or "")
                    for profile in strata_model_profiles()
                    if isinstance(profile, dict) and profile.get("id")
                ),
            },
        }
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def _run_coalesced_normal_catalog(
        operation: Callable[..., Any],
        snapshot_key: str,
        *args: Any,
    ) -> Any:
        key = snapshot_key
        worker = normal_catalog_tasks.get(key)
        if worker is None or worker.done():
            worker = asyncio.create_task(
                asyncio.to_thread(operation, *args)
            )
            normal_catalog_tasks[key] = worker

            def _consume_failed_worker(done: asyncio.Task[Any]) -> None:
                if not done.cancelled():
                    done.exception()

            worker.add_done_callback(_consume_failed_worker)
        try:
            return await asyncio.shield(worker)
        finally:
            if worker.done() and normal_catalog_tasks.get(key) is worker:
                normal_catalog_tasks.pop(key, None)

    async def _run_coalesced_deepinfra_catalog(
        operation: Callable[..., Any],
        snapshot_key: str,
        *args: Any,
    ) -> Any:
        """Share one DeepInfra registry fetch for a config snapshot."""

        worker = deepinfra_catalog_tasks.get(snapshot_key)
        if worker is None or worker.done():
            worker = asyncio.create_task(asyncio.to_thread(operation, *args))
            deepinfra_catalog_tasks[snapshot_key] = worker

            def _consume_failed_deepinfra_worker(done: asyncio.Task[Any]) -> None:
                if not done.cancelled():
                    done.exception()

            worker.add_done_callback(_consume_failed_deepinfra_worker)
        try:
            return await asyncio.shield(worker)
        finally:
            if worker.done() and deepinfra_catalog_tasks.get(snapshot_key) is worker:
                deepinfra_catalog_tasks.pop(snapshot_key, None)

    async def _drain_runtime_discovery(worker: asyncio.Task[Any]) -> Any:
        """Await a discovery worker to completion even after request cancel."""

        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError as exc:
                if worker.cancelled():
                    raise
                cancelled = cancelled or exc
            except Exception:
                if cancelled is None:
                    raise
                break
        if cancelled is not None:
            raise cancelled
        return result

    async def _run_bounded_runtime_discovery(
        operation: Callable[..., Any], *args: Any
    ) -> Any:
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    runtime_discovery_gate.acquire(),
                    timeout=runtime_discovery_queue_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise HTTPException(
                    status_code=429,
                    detail="LLM runtime discovery is busy; retry shortly",
                    headers={"Retry-After": "1"},
                ) from exc
            acquired = True
            worker = asyncio.create_task(asyncio.to_thread(operation, *args))
            return await _drain_runtime_discovery(worker)
        finally:
            if acquired:
                runtime_discovery_gate.release()

    async def _resolve_request_user_id(request: Request) -> str | None:
        user_resolver = getattr(server, "_get_user_info_from_request", None)
        if not callable(user_resolver):
            return None
        try:
            user_info = await user_resolver(request)
        except Exception:  # pragma: no cover - auth backend failure
            return None
        if not isinstance(user_info, dict):
            return None
        actor_id = user_info.get("id") or user_info.get("user_id")
        return str(actor_id).strip() if actor_id else None

    async def _require_global_config_admin(
        request: Request, _: None = Depends(require_auth)
    ) -> None:
        if getattr(server, "auth_enabled", True) is False:
            return
        if not await server._is_admin_user(request):
            raise HTTPException(
                status_code=403,
                detail="Administrator privileges required",
            )

    async def _require_session_settings_access(
        request: Request,
        session_id: str,
        *,
        require_write: bool,
    ) -> None:
        if getattr(server, "auth_enabled", True) is False:
            return
        user_id = await _resolve_request_user_id(request)
        if not user_id:
            raise HTTPException(status_code=404, detail="Session not found")
        is_admin = await server._is_admin_user(request)
        allowed = await server._websocket_session_allowed(
            session_id,
            user_id,
            require_write=require_write,
            is_admin=is_admin,
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="Session not found")

    @app.get("/api/llm/local-runtime/status")
    async def get_local_runtime_status(
        model: str = Query(..., min_length=1),
        _: None = Depends(require_auth),
    ):
        model_id = str(model or "").strip()
        if not model_id:
            raise HTTPException(status_code=400, detail="model は必須です")

        def _llama_cpp_auto_start_enabled() -> bool:
            """Read the launch toggle without resolving or probing a runtime."""

            value: Any = None
            getter = getattr(server.config, "get", None)
            if callable(getter):
                try:
                    value = getter(
                        "openai_compatible_local.llama_cpp.auto_start",
                        None,
                    )
                except TypeError:
                    value = None
            if value is None and isinstance(server.config, dict):
                local = server.config.get("openai_compatible_local") or {}
                llama = local.get("llama_cpp") if isinstance(local, dict) else {}
                if isinstance(llama, dict):
                    value = llama.get("auto_start")
            if value is None:
                value = os.getenv("LLAMA_CPP_AUTO_START")
            if isinstance(value, bool):
                return value
            if value is None:
                return True
            return str(value).strip().lower() not in {"0", "false", "no", "off"}

        def _llama_cpp_process_snapshot(
            endpoint: str,
            expected_alias: str,
        ) -> tuple[object | None, bool, bool]:
            """Find one tracked worker and validate its controller identity."""

            from src.service_manager import (
                _openai_compatible_local_processes,
                _owned_llama_cpp_process_at,
            )

            process = None
            try:
                process = _owned_llama_cpp_process_at(endpoint, expected_alias)
            except Exception:
                process = None
            if process is None:
                for candidate in list(_openai_compatible_local_processes):
                    recorded_endpoint = str(
                        getattr(candidate, "_aoi_local_base_url", None)
                        or getattr(candidate, "_aoi_llama_cpp_base_url", "")
                        or ""
                    ).strip()
                    if not recorded_endpoint:
                        continue
                    try:
                        same_endpoint = (
                            normalize_openai_compatible_base_url(recorded_endpoint)
                            == endpoint
                        )
                    except Exception:
                        same_endpoint = False
                    if (
                        same_endpoint
                        and str(
                            getattr(candidate, "_aoi_local_runtime", "") or ""
                        )
                        == "llama_cpp"
                    ):
                        process = candidate
                        break
            if process is None:
                return None, False, False
            try:
                process_live = process.poll() is None
            except Exception:
                process_live = False
            process_runtime = str(
                getattr(process, "_aoi_local_runtime", "") or ""
            )
            process_alias = str(
                getattr(process, "_aoi_local_model_alias", "") or ""
            ).strip()
            process_endpoint = str(
                getattr(process, "_aoi_local_base_url", None)
                or getattr(process, "_aoi_llama_cpp_base_url", "")
                or ""
            ).strip()
            try:
                process_endpoint = normalize_openai_compatible_base_url(
                    process_endpoint
                )
            except Exception:
                process_endpoint = ""
            identity_ok = bool(
                process_runtime == "llama_cpp"
                and process_endpoint == endpoint
                and process_alias
                and process_alias == expected_alias
            )
            return process, process_live, identity_ok

        try:
            state = None
            owned_process = None
            strata_readiness = None
            llama_cpp_process = None
            llama_cpp_endpoint = None
            llama_cpp_expected_alias = None
            llama_cpp_process_live = False
            llama_cpp_process_identity_ok = False
            managed_runtime = managed_local_runtime_for_model(server.config, model_id)
            managed_ownership = provider_runtime_ownership(
                "openai_compatible_local",
                server.config,
                model=model_id,
            )
            if (
                managed_runtime == "strata"
                and managed_ownership.managed_runtime
            ):
                from src.services.strata_runtime import strata_base_url, strata_readiness

                try:
                    from src.service_manager import _owned_strata_process_at

                    owned_process = _owned_strata_process_at(
                        strata_base_url(server.config, model=model_id)
                    )
                except Exception:
                    owned_process = None
                fast_status = getattr(
                    local_runtime_manager,
                    "status_for_owned_strata",
                    None,
                )
                if owned_process is not None and callable(fast_status):
                    state = await asyncio.to_thread(
                        fast_status,
                        model_id,
                        owned_process,
                    )

            if managed_runtime == "llama_cpp" and managed_ownership.managed_runtime:
                cpp_profile = llama_cpp_model_profile(model_id)
                if cpp_profile is not None:
                    llama_cpp_expected_alias = str(
                        cpp_profile.get("served_alias")
                        or cpp_profile.get("id")
                        or model_id
                    ).strip()
                try:
                    llama_cpp_endpoint = normalize_openai_compatible_base_url(
                        openai_compatible_local_base_url(
                            server.config,
                            model=model_id,
                        )
                    )
                    (
                        llama_cpp_process,
                        llama_cpp_process_live,
                        llama_cpp_process_identity_ok,
                    ) = await asyncio.to_thread(
                        _llama_cpp_process_snapshot,
                        llama_cpp_endpoint,
                        llama_cpp_expected_alias or model_id,
                    )
                    if llama_cpp_process_live and llama_cpp_process_identity_ok:
                        fast_status = getattr(
                            local_runtime_manager,
                            "status_for_owned_llama_cpp",
                            None,
                        )
                        if callable(fast_status):
                            state = await asyncio.to_thread(
                                fast_status,
                                model_id,
                                llama_cpp_process,
                            )
                except Exception:
                    # Status is diagnostic. A malformed/stale process record
                    # falls through to the cheap artifact snapshot below.
                    llama_cpp_process = None

            if not isinstance(state, dict):
                poll_status = getattr(local_runtime_manager, "status_for_poll", None)
                if callable(poll_status):
                    state = await asyncio.to_thread(
                        poll_status,
                        model_id,
                    )
                else:
                    state = await asyncio.to_thread(
                        local_runtime_manager.status,
                        model_id,
                    )

            if (
                managed_runtime == "llama_cpp"
                and managed_ownership.managed_runtime
                and llama_cpp_endpoint
            ):
                if llama_cpp_process is None:
                    prepared = bool(state.get("prepared"))
                    auto_start_enabled = _llama_cpp_auto_start_enabled()
                    endpoint_probe = "unknown"
                    if prepared and auto_start_enabled:
                        endpoint_probe = await asyncio.to_thread(
                            _probe_llama_cpp_endpoint,
                            llama_cpp_endpoint,
                        )
                    waiting = (
                        prepared
                        and auto_start_enabled
                        and endpoint_probe == "closed"
                    )
                    state["server"] = {
                        "status": "waiting" if waiting else "offline",
                        "running": False,
                        "ready": False,
                        "base_url": llama_cpp_endpoint,
                        "endpoint_offline": endpoint_probe == "closed",
                    }
                elif not llama_cpp_process_live:
                    state["status"] = "failed"
                    state["phase"] = "runtime"
                    state["done"] = True
                    state["error"] = "llama.cpp server exited before readiness."
                    state["server"] = {
                        "status": "failed",
                        "running": False,
                        "ready": False,
                        "base_url": llama_cpp_endpoint,
                        "endpoint_offline": True,
                    }
                elif not llama_cpp_process_identity_ok:
                    state["status"] = "failed"
                    state["phase"] = "runtime"
                    state["done"] = True
                    state["error"] = "llama.cpp server identity does not match."
                    state["server"] = {
                        "status": "failed",
                        "running": True,
                        "ready": False,
                        "base_url": llama_cpp_endpoint,
                    }
                else:
                    from src.service_manager._local_llm_servers import (
                        _llama_cpp_model_ids_exact,
                    )

                    try:
                        served_ids = await asyncio.to_thread(
                            _llama_cpp_model_ids_exact,
                            llama_cpp_endpoint,
                        )
                    except Exception:
                        served_ids = set()
                    expected_alias = llama_cpp_expected_alias or model_id
                    if expected_alias in served_ids:
                        server_status = "ready"
                        server_ready = True
                    elif served_ids:
                        # A live worker serving another alias is an explicit
                        # identity failure. Preserve the requested alias in
                        # the state while refusing to claim readiness.
                        state["status"] = "failed"
                        state["phase"] = "runtime"
                        state["done"] = True
                        state["error"] = "llama.cpp server model alias mismatch."
                        server_status = "failed"
                        server_ready = False
                    else:
                        server_status = "loading"
                        server_ready = False
                    state["server"] = {
                        "status": server_status,
                        "running": True,
                        "ready": server_ready,
                        "base_url": llama_cpp_endpoint,
                    }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            logger.exception("Managed local runtime status failed")
            raise HTTPException(
                status_code=500,
                detail="Failed to inspect managed local runtime",
            ) from None
        if state.get("runtime") == "strata":
            if strata_readiness is None:
                from src.services.strata_runtime import strata_readiness
            state["server"] = await asyncio.to_thread(
                strata_readiness,
                server.config,
                model=model_id,
                owned_process=owned_process,
            )
        return JSONResponse(_local_runtime_api_payload(state, server.config))

    @app.post("/api/llm/local-runtime/prepare")
    @_guard_llm_runtime_change
    async def prepare_local_runtime(
        request: Request,
        _: None = Depends(_require_global_config_admin),
    ):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail="JSON objectを指定してください",
            ) from exc
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail="JSON objectを指定してください",
            )

        model_id = str(body.get("model") or "").strip()
        if not model_id:
            raise HTTPException(status_code=400, detail="model は必須です")

        runtime_value = body.get("runtime")
        if runtime_value is not None and not isinstance(runtime_value, str):
            raise HTTPException(
                status_code=400,
                detail="runtime は文字列で指定してください",
            )
        runtime = (
            str(runtime_value).strip()
            if isinstance(runtime_value, str) and runtime_value.strip()
            else None
        )

        is_strata = managed_local_runtime_for_model(server.config, model_id) == "strata"
        setup_kwargs: dict[str, Any] = {}
        if is_strata:
            from src.services.strata_runtime import normalize_strata_settings, strata_external, strata_settings

            if strata_external(server.config):
                raise HTTPException(status_code=409, detail="EnterpriseのStrataはoperator管理です。AoiTalkからsetupしません")
            if runtime not in (None, "strata"):
                raise HTTPException(status_code=400, detail="Strata runtimeが一致しません")
            action = body.get("action", "setup")
            if action in {"start", "stop"}:
                from src.service_manager import ensure_openai_compatible_local_server, stop_strata_server
                from src.services.strata_runtime import strata_readiness

                if not strata_settings(server.config, model=model_id)["auto_start"]:
                    raise HTTPException(status_code=409, detail="外部Strataはoperator管理です")
                if action == "start":
                    await _run_llm_runtime_change_async(ensure_openai_compatible_local_server, server.config, model=model_id, raise_on_launch_error=True)
                else:
                    await _run_llm_runtime_change_async(stop_strata_server, server.config, model=model_id)
                state = await asyncio.to_thread(local_runtime_manager.status, model_id, "strata")
                state["server"] = await asyncio.to_thread(strata_readiness, server.config, model=model_id)
                return JSONResponse(_local_runtime_api_payload(state, server.config))
            if action not in {"setup", "configure"}:
                raise HTTPException(status_code=400, detail="Strata actionが不正です")
            configure_only = body.get("configure_only") is True or action == "configure"
            if not configure_only and body.get("setup_confirmed") is not True:
                raise HTTPException(status_code=400, detail="公式setup実行・大容量downloadの確認が必要です")
            try:
                settings = normalize_strata_settings(body.get("strata", {}))
                changes = {f"openai_compatible_local.strata.{key}": value for key, value in settings.items()}
                endpoint = body.get("base_url")
                if endpoint is not None:
                    from urllib.parse import urlsplit

                    parsed = urlsplit(str(endpoint))
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                        raise ValueError("Strata外部endpointは認証情報を含まないhttp(s) URLで指定してください")
                    changes["openai_compatible_local.base_url"] = str(endpoint)
                staged = _ConfigOverlay(server.config, changes)
                effective = strata_settings(staged, model=model_id)
                # Preserve known successful engine performance flags when a
                # path/config change is saved.  The official setup otherwise
                # regenerates --prefill and --vram-reserve-mib with its own
                # defaults, while Strata's generated JSON is not the durable
                # AoiTalk settings store.
                for performance_key in ("prefill", "vram_reserve_mib", "expert_cache"):
                    if (
                        performance_key not in settings
                        and effective.get(performance_key) is not None
                    ):
                        changes[
                            f"openai_compatible_local.strata.{performance_key}"
                        ] = effective[performance_key]
                if not configure_only and not effective["auto_start"]:
                    raise ValueError("外部Strataのsetupはoperatorが行ってください")
                if not configure_only and not settings.get("data_dir") and not server.config.get("openai_compatible_local.strata.data_dir", ""):
                    raise ValueError("大容量データの保存先（repository外）を明示してください")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if configure_only:
                try:
                    await _run_llm_runtime_change_async(
                        _apply_strata_runtime_change,
                        server,
                        model=model_id,
                        changes=changes,
                        require_prepared=True,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                state = await asyncio.to_thread(local_runtime_manager.status, model_id, "strata")
                return JSONResponse(_local_runtime_api_payload(state, server.config))
            try:
                from src.service_manager import validate_strata_setup_safety

                validate_strata_setup_safety(server.config, model=model_id)
            except (OSError, RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            setup_kwargs["setup_confirmed"] = True
            setup_kwargs["strata_settings_override"] = effective
            setup_kwargs["on_success"] = lambda: _commit_strata_runtime_change(
                server,
                model=model_id,
                changes=changes,
                require_prepared=True,
            )

        try:
            state = await asyncio.to_thread(
                local_runtime_manager.start_prepare,
                model_id,
                runtime,
                **setup_kwargs,
            )
        except ValueError as exc:
            # Includes unknown/untrusted profiles and explicit
            # profile/runtime mismatches.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            logger.exception("Managed local runtime prepare failed to start")
            raise HTTPException(
                status_code=500,
                detail="Failed to start managed local runtime preparation",
            ) from None
        return JSONResponse(_local_runtime_api_payload(state, server.config))

    @app.get("/api/llm/local-runtime/tasks/{task_id}")
    async def get_local_runtime_task(
        task_id: str,
        _: None = Depends(require_auth),
    ):
        clean_task_id = str(task_id or "").strip()
        if not clean_task_id:
            raise HTTPException(status_code=400, detail="task_id は必須です")
        try:
            state = await asyncio.to_thread(
                local_runtime_manager.get_task,
                clean_task_id,
            )
        except Exception:
            logger.exception("Managed local runtime task lookup failed")
            raise HTTPException(
                status_code=500,
                detail="Failed to inspect managed local runtime task",
            ) from None

        payload = _local_runtime_api_payload(state, server.config)
        return JSONResponse(
            payload,
            status_code=404 if payload["status"] == "not_found" else 200,
        )

    @app.post("/api/llm/local-runtime/tasks/{task_id}/cancel")
    async def cancel_local_runtime_task(
        request: Request,
        task_id: str,
        _: None = Depends(require_auth),
    ):
        await _require_global_config_admin(request)
        clean_task_id = str(task_id or "").strip()
        if not clean_task_id:
            raise HTTPException(status_code=400, detail="task_id は必須です")
        try:
            state = await asyncio.to_thread(
                local_runtime_manager.cancel_prepare,
                clean_task_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            logger.exception("Managed local runtime task cancellation failed")
            raise HTTPException(
                status_code=500,
                detail="Failed to cancel managed local runtime preparation",
            ) from None
        payload = _local_runtime_api_payload(state, server.config)
        return JSONResponse(
            payload,
            status_code=404 if payload["status"] == "not_found" else 200,
        )

    @app.get("/api/llm/llama-cpp/model-root")
    async def get_llama_cpp_model_root(_: None = Depends(require_auth)):
        """Return the effective canonical GGUF model root and provenance."""

        try:
            return JSONResponse(_llama_cpp_model_root_payload(server.config))
        except ValueError as exc:
            # A stale persisted path should be visible as a client/config
            # error, not leak a raw traceback or cause an unrelated 500.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/llm/llama-cpp/model-root")
    async def put_llama_cpp_model_root(
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Persist one canonical model root; empty input restores the default."""

        await _require_global_config_admin(request)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail="JSON objectを指定してください",
            ) from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON objectを指定してください")
        if "model_root" in body:
            raw_value = body.get("model_root")
        elif "path" in body:
            # ``path`` was used by one early settings client; retain this
            # narrow alias while keeping model_root the canonical contract.
            raw_value = body.get("path")
        else:
            raise HTTPException(status_code=400, detail="model_root は必須です")
        if raw_value is not None and not isinstance(raw_value, str):
            raise HTTPException(status_code=400, detail="model_root は文字列またはnullで指定してください")
        try:
            canonical = canonicalize_llama_cpp_model_root_override(
                raw_value,
                create=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _persist_config_changes(
            server.config,
            {
                "openai_compatible_local.llama_cpp.model_root": str(canonical or ""),
            },
        )
        try:
            return JSONResponse(
                _llama_cpp_model_root_payload(server.config, success=True)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── LLM Mode API Endpoints ──────────────────────────────────────────
    @app.get("/api/llm/mode")
    async def get_llm_mode():
        """Get current LLM response mode or reasoning effort."""
        return JSONResponse(
            build_llm_mode_state(server.config, client=server._llm_client)
        )

    @app.post("/api/ai/docs/command")
    async def run_docs_ai_command(request: Request, _: None = Depends(require_auth)):
        """Run Docs AI commands through the configured LLM client."""
        body = await request.json()
        command = str(body.get("command") or "continue").strip()[:80]
        prompt = str(body.get("prompt") or "").strip()[:8000]
        node_id = str(body.get("node_id") or "").strip()[:80] or None

        llm_client = server._llm_client
        if llm_client is None:
            raise HTTPException(status_code=503, detail="LLM client is not configured")

        mode_instruction = {
            "continue": "Return child outline lines that naturally continue the selected Docs node.",
            "extract_tasks": "Extract actionable task lines. Each line must be a task title.",
            "rewrite": "Rewrite the selected node title. Return replacement only in JSON.",
            "fill_fields": "Suggest field values for empty Docs fields.",
        }.get(command, "Return child outline lines that naturally continue the selected Docs node.")
        json_shape = (
            '{"mode":"replace_title","replacement":"...","summary":"..."}'
            if command == "rewrite"
            else '{"mode":"field_suggestions","fields":[{"name":"...","value":"..."}],"summary":"..."}'
            if command == "fill_fields"
            else '{"mode":"insert_children","lines":["..."],"summary":"..."}'
        )
        # Cookie/Bearer auth validates the request but does not necessarily
        # populate ``request.state.user``.  Resolve the same authenticated
        # principal used by the other API routes before constructing a Docs
        # prompt; falling back to state keeps lightweight test/fake servers
        # compatible without weakening the ACL when the resolver is present.
        actor_id = None
        user_resolver = getattr(server, "_get_user_info_from_request", None)
        if callable(user_resolver):
            try:
                user_info = await user_resolver(request)
            except Exception:  # pragma: no cover - auth backend failure
                user_info = None
            if isinstance(user_info, dict):
                actor_id = user_info.get("id") or user_info.get("user_id")
        if actor_id is None:
            state_user = getattr(getattr(request, "state", None), "user", None)
            if isinstance(state_user, dict):
                actor_id = state_user.get("id") or state_user.get("user_id")
            else:
                actor_id = getattr(state_user, "id", None) if state_user is not None else None
        docs_context = await _build_docs_ai_context(node_id, actor_id)
        llm_prompt = (
            "You are AoiTalk Docs AI. Respond with strict JSON only, no markdown.\n"
            f"Command: {command}\n"
            f"Node ID: {node_id or ''}\n"
            f"Instruction: {mode_instruction}\n"
            f"Required JSON shape: {json_shape}\n"
            "Write concise Japanese content unless the user prompt clearly uses another language.\n"
            "User/selection context:\n"
            f"{prompt or '(empty)'}\n\n"
            f"{docs_context}"
        )
        try:
            raw = await _run_docs_ai_completion(llm_client, llm_prompt)
            parsed = _extract_json_object(raw)
            result = _normalize_docs_ai_result(command, parsed)
        except Exception as exc:
            logger.exception("Docs AI command failed")
            raise HTTPException(status_code=502, detail="Docs AI generation failed", headers={"x-error": str(exc)})
        return JSONResponse({"result": result, "confidence": 0.78})

    @app.post("/api/llm/mode")
    @_guard_llm_runtime_change
    async def set_llm_mode(request: Request, _: None = Depends(_require_global_config_admin)):
        """Set the current LLM response mode or reasoning effort."""
        try:
            body = await request.json()
            mode = str(body.get("mode", "")).strip()
            state = build_llm_mode_state(server.config, client=server._llm_client)
            available_modes = state.get("available_modes") or []

            if mode not in available_modes:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid mode. Use one of: {', '.join(available_modes)}",
                )

            provider = str(state.get("provider") or "").strip()
            kind = str(state.get("kind") or "response_mode")
            config_changes: dict[str, Any] = {}
            old_client = server._llm_client
            old_runtime_mode = server._current_llm_mode

            if kind == "reasoning_effort":
                if provider == "codex-cli":
                    config_changes["codex_cli.reasoning_effort"] = mode
                elif provider == "claude-cli":
                    config_changes["claude_cli.reasoning_effort"] = mode
                elif provider == "openai":
                    config_changes["openai.reasoning_effort"] = mode
                elif provider == "deepseek":
                    config_changes["deepseek.reasoning_effort"] = mode
                elif provider == "deepinfra":
                    config_changes["deepinfra.reasoning_effort"] = mode
                elif provider == "kimi" and mode == "max":
                    config_changes["kimi.reasoning_effort"] = "max"
                elif provider == "openai_compatible_local":
                    # Only managed Qwen3.8 profiles have a reasoning-effort
                    # contract.  Generic local-model keeps fast/thinking.
                    config_changes[
                        "openai_compatible_local.llama_cpp.reasoning_effort"
                    ] = mode
                config_changes["llm_runtime_mode"] = mode

                staged_config = _ConfigOverlay(server.config, config_changes)
                from ...llm.manager import create_llm_client

                # Construct against a read-only candidate first.  A failed
                # preflight/client build must not alter DB or live config.
                new_client = create_llm_client(staged_config)
                next_state = build_llm_mode_state(staged_config, client=new_client)
                # The production setter assigns first and absorbs callback
                # failures.  Install the ready client before DB commit so a
                # persistence failure can restore the old client without any
                # competing configuration rollback write.
                try:
                    server.set_llm_client(new_client)
                except Exception:
                    _restore_llm_client(server, old_client)
                    raise
                try:
                    _persist_config_changes(server.config, config_changes)
                except Exception:
                    _restore_llm_client(server, old_client)
                    server._current_llm_mode = old_runtime_mode
                    raise
            elif server._llm_client and hasattr(server._llm_client, "set_llm_mode"):
                previous_client_mode = str(state.get("mode") or old_runtime_mode)
                try:
                    server._llm_client.set_llm_mode(mode)
                except Exception:
                    try:
                        server._llm_client.set_llm_mode(previous_client_mode)
                    except Exception:
                        logger.exception("Failed to restore previous LLM mode")
                    raise
                logger.info(f"LLM mode set to: {mode}")

            try:
                if kind != "reasoning_effort":
                    _persist_config_changes(
                        server.config,
                        {"llm_runtime_mode": mode},
                    )
            except Exception:
                if server._llm_client and hasattr(
                    server._llm_client,
                    "set_llm_mode",
                ):
                    try:
                        server._llm_client.set_llm_mode(previous_client_mode)
                    except Exception:
                        logger.exception("Failed to restore previous LLM mode")
                server._current_llm_mode = old_runtime_mode
                raise

            server._current_llm_mode = mode
            if kind != "reasoning_effort":
                next_state = build_llm_mode_state(
                    server.config,
                    client=server._llm_client,
                )

            # Broadcast mode change to matching WebSocket clients
            try:
                await broadcast_llm_state_change(
                    server,
                    request,
                    {"type": "llm_mode_change", "data": next_state},
                )
            except Exception:
                # The switch is already committed.  Notification failure must
                # not report the successfully-applied mode as an API failure.
                logger.warning("Failed to broadcast LLM mode change", exc_info=True)

            return JSONResponse(
                {
                    "success": True,
                    **next_state,
                    "message": f"LLM設定を {mode} に切り替えました",
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to set LLM mode: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    def _build_model_catalog(
        cfg,
        include_remote: bool = False,
        refresh_provider: Optional[str] = None,
        cached_catalog: Optional[Dict[str, Any]] = None,
        include_runtime_models: bool = False,
        include_installed_local_models: bool = False,
    ):
        return build_llm_model_catalog(
            cfg,
            ollama_model_manager=server._ollama_model_manager,
            include_remote=include_remote,
            refresh_provider=refresh_provider,
            cached_catalog=cached_catalog,
            include_runtime_models=include_runtime_models,
            include_installed_local_models=include_installed_local_models,
        )

    def _build_engine_list(cfg):
        """APIキーの有無でフィルタリングした利用可能エンジン一覧を返す"""
        return build_llm_engine_options(
            cfg,
            ollama_model_manager=server._ollama_model_manager,
        )

    def _deepinfra_catalog_key(config: Any) -> str:
        return _normal_catalog_snapshot_key(config, "deepinfra")

    def _deepinfra_retry_active(snapshot_key: str) -> bool:
        return deepinfra_refresh_retry_until.get(snapshot_key, 0.0) > time.monotonic()

    def _annotate_deepinfra_failure(catalog: Dict[str, Any], snapshot_key: str) -> None:
        error = deepinfra_refresh_errors.get(snapshot_key)
        if not error or not isinstance(catalog, dict):
            return
        providers = catalog.get("providers")
        if not isinstance(providers, list):
            return
        for provider_payload in providers:
            if isinstance(provider_payload, dict) and provider_payload.get("id") == "deepinfra":
                # Keep the stale/static model list visible while making the
                # failed refresh explicit to the settings UI.
                provider_payload["error"] = error

    async def _persist_catalog_provider_cache(
        cached_catalog: Dict[str, Any],
        provider: str,
        catalog: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist one successful provider refresh without lost updates."""

        providers = catalog.get("providers") if isinstance(catalog, dict) else None
        if not isinstance(providers, list):
            return cached_catalog
        refreshed_provider = next(
            (
                item
                for item in providers
                if isinstance(item, dict) and item.get("id") == provider
            ),
            None,
        )
        if not refreshed_provider:
            return cached_catalog
        if provider == "deepinfra" and (
            refreshed_provider.get("refreshed") is not True
            or refreshed_provider.get("source") != "remote"
        ):
            # A blocked deployment or an unconfigured custom endpoint can
            # yield an empty/static provider payload without a successful
            # remote snapshot. Never let that clear a good dynamic cache.
            return cached_catalog

        # Reload while holding the process-local lock so simultaneous refreshes
        # of different providers merge into the latest cache snapshot instead
        # of overwriting one another with an older request-local copy.
        async with model_catalog_cache_write_lock:
            latest_cache = await asyncio.to_thread(load_model_catalog_cache)
            if not isinstance(latest_cache, dict):
                latest_cache = cached_catalog if isinstance(cached_catalog, dict) else {}
            next_cache = await asyncio.to_thread(
                update_model_catalog_cache,
                latest_cache,
                provider,
                refreshed_provider.get("models") or [],
                allow_empty_remote=provider == "deepinfra",
            )
            if next_cache == latest_cache:
                return latest_cache
            await asyncio.to_thread(save_model_catalog_cache, next_cache)
            refreshed_at = cached_provider_updated_at(provider, next_cache)

        # The response came from the provider just now.  Keep that remote
        # provenance in this response instead of rebuilding from cache and
        # labelling the fresh list as a previous retrieval.
        if refreshed_at:
            refreshed_provider["cached_at"] = refreshed_at
        return next_cache

    @app.get("/api/llm/models")
    async def get_llm_models(
        refresh: bool = Query(False),
        provider: Optional[str] = Query(None),
        _: None = Depends(require_auth),
    ):
        """Return provider-grouped model options for the settings screen."""
        started_at = time.perf_counter()
        # Catalog construction and cache I/O are synchronous. Keep them off the
        # FastAPI event loop; normal reads use a read-only local inventory so an
        # installed model is selectable before its first request. HTTP runtime
        # discovery remains an explicit refresh operation; a slow local
        # endpoint must not hide the disk inventory behind the request timeout.
        # Positional arguments keep this safe for lightweight test doubles.
        cached_catalog = await asyncio.to_thread(load_model_catalog_cache)
        deepinfra_key = _deepinfra_catalog_key(server.config)
        deepinfra_auto_refresh = (
            not refresh
            and provider in {None, "deepinfra"}
            and (
                not cached_provider_has_snapshot("deepinfra", cached_catalog)
                or not cached_provider_is_fresh(
                    "deepinfra",
                    cached_catalog,
                    ttl_seconds=DEEPINFRA_MODEL_CATALOG_TTL_SECONDS,
                )
            )
            and not _deepinfra_retry_active(deepinfra_key)
        )
        if refresh and provider == "deepinfra":
            # DeepInfra's public registry does not need local runtime probes;
            # use a dedicated coalesced worker so concurrent refreshes share
            # one request and cannot be rejected by the local discovery gate.
            catalog = await _run_coalesced_deepinfra_catalog(
                _build_model_catalog,
                deepinfra_key,
                server.config,
                True,
                provider,
                cached_catalog,
                False,
                True,
            )
        elif refresh:
            catalog = await _run_bounded_runtime_discovery(
                _build_model_catalog,
                server.config,
                refresh,
                provider,
                cached_catalog,
                True,
                False,
            )
        elif deepinfra_auto_refresh:
            catalog = await _run_coalesced_deepinfra_catalog(
                _build_model_catalog,
                deepinfra_key,
                server.config,
                True,
                "deepinfra",
                cached_catalog,
                False,
                True,
            )
        else:
            catalog = await _run_coalesced_normal_catalog(
                _build_model_catalog,
                _normal_catalog_snapshot_key(server.config, provider),
                server.config,
                refresh,
                provider,
                cached_catalog,
                False,
                True,
            )
        refresh_targets: list[str] = []
        if refresh:
            if provider:
                refresh_targets = [provider]
            else:
                refresh_targets = [
                    str(item.get("id") or "")
                    for item in (catalog.get("providers") or [])
                    if isinstance(item, dict)
                    and str(item.get("id") or "") in CACHEABLE_REMOTE_PROVIDERS
                ]
        elif deepinfra_auto_refresh:
            refresh_targets = ["deepinfra"]

        if refresh_targets:
            catalog_providers = catalog.get("providers") if isinstance(catalog, dict) else []
            for refresh_target in refresh_targets:
                target_provider = next(
                    (
                        item
                        for item in (catalog_providers or [])
                        if isinstance(item, dict) and item.get("id") == refresh_target
                    ),
                    None,
                )
                target_error = target_provider.get("error") if target_provider else None
                if refresh_target == "deepinfra":
                    if target_error:
                        deepinfra_refresh_retry_until[deepinfra_key] = (
                            time.monotonic() + DEEPINFRA_MODEL_CATALOG_RETRY_SECONDS
                        )
                        deepinfra_refresh_errors[deepinfra_key] = (
                            DEEPINFRA_MODEL_CATALOG_ERROR
                        )
                    else:
                        deepinfra_refresh_retry_until.pop(deepinfra_key, None)
                        deepinfra_refresh_errors.pop(deepinfra_key, None)
                if target_provider and not target_error:
                    try:
                        await _persist_catalog_provider_cache(
                            cached_catalog,
                            refresh_target,
                            catalog,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to save LLM model catalog cache: %s",
                            type(exc).__name__,
                        )
        elif not refresh:
            _annotate_deepinfra_failure(catalog, deepinfra_key)
        return _attach_server_timing(
            JSONResponse(catalog),
            "aoi_llm_models",
            started_at,
        )

    @app.get("/api/llm/openrouter/provider-routing")
    async def get_openrouter_provider_routing(
        model: str = Query(..., min_length=1),
        _: None = Depends(require_auth),
    ):
        model_id = str(model or "").strip()
        try:
            candidates = await asyncio.to_thread(
                fetch_provider_candidates,
                server.config,
                model_id,
            )
        except Exception as exc:
            logger.warning("OpenRouter provider候補の取得に失敗しました: %s", exc)
            raise HTTPException(
                status_code=502,
                detail=f"OpenRouter provider候補の取得に失敗しました: {exc}",
            )
        return JSONResponse(
            {
                "model": model_id,
                "provider": provider_options_for_model(server.config, model_id),
                "providers": candidates,
            }
        )

    @app.patch("/api/llm/openrouter/provider-routing")
    async def update_openrouter_provider_routing(
        request: Request,
        _: None = Depends(require_auth),
    ):
        await _require_global_config_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON objectを指定してください")
        model_id = str(body.get("model") or "").strip()
        if not model_id:
            raise HTTPException(status_code=400, detail="model は必須です")
        try:
            provider = normalize_provider_options(body.get("provider"), strict=True)
            current = normalize_model_provider_options(
                server.config.get(MODEL_PROVIDER_OPTIONS_CONFIG_KEY, {}) or {}
            )
            if provider:
                current[model_id] = provider
            else:
                current.pop(model_id, None)
            saver = getattr(server.config, "save_to_file", None)
            if callable(saver):
                if not saver(MODEL_PROVIDER_OPTIONS_CONFIG_KEY, current):
                    raise RuntimeError("OpenRouter provider設定の保存に失敗しました")
            else:
                server.config.set(MODEL_PROVIDER_OPTIONS_CONFIG_KEY, current)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "OpenRouter provider設定の保存に失敗しました: exception_type=%s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to save OpenRouter provider settings",
            ) from None
        return JSONResponse(
            {
                "success": True,
                "model": model_id,
                "provider": provider,
            }
        )

    @app.get("/api/llm/engine")
    async def get_llm_engine(
        include_available: bool = Query(False),
    ):
        """現在のLLMエンジン情報を返す（候補探索は明示時だけ実行する）"""
        started_at = time.perf_counter()
        provider = server.config.get("llm_provider", "openai")
        model = server.config.get("llm_model", "gpt-4o")
        deployment = resolve_llm_deployment(server.config)
        deployment_payload = deployment.metadata() if deployment else None
        response = {
            "provider": provider,
            "model": model,
            "persisted_provider": provider,
            "persisted_model": model,
            "available_included": include_available,
            "execution_profile": execution_profile_envelope(server.config),
            "effective_main": resolve_execution_main_route(server.config),
        }
        if include_available:
            # Runtime discovery can wait for a managed server that is still
            # loading a model. Keep it an explicit opt-in so a metadata read
            # never holds the request open during a cold start.
            response["available"] = await _run_bounded_runtime_discovery(
                _build_engine_list,
                server.config,
            )
        else:
            # Keep the response shape stable for consumers that always parse
            # ``available`` while explicitly signalling that discovery was
            # skipped via ``available_included`` above.
            response["available"] = []
        if deployment_payload is not None:
            response["deployment"] = deployment_payload
            response["effective_provider"] = deployment.effective_provider
            response["effective_model"] = deployment.effective_model
        if str(provider).strip().lower() == "openai_compatible_local":
            try:
                response["llama_cpp"] = _llama_cpp_model_root_payload(server.config)
            except ValueError:
                # Keep the legacy engine envelope available even when an old
                # persisted path is stale; the dedicated model-root endpoint
                # reports the actionable validation error.
                pass
        return _attach_server_timing(
            JSONResponse(response),
            "aoi_llm_engine",
            started_at,
        )

    @app.post("/api/llm/engine")
    @_guard_llm_runtime_change
    async def set_llm_engine(request: Request, _: None = Depends(_require_global_config_admin)):
        """Switch the active LLM engine (provider/model)."""
        started_at = time.perf_counter()
        try:
            body = await request.json()
            provider = str(body.get("provider", "")).strip()
            model = str(body.get("model", "")).strip()
            base_url = body.get("base_url")
            refresh_runtime = body.get("refresh_runtime", False)
            if not isinstance(refresh_runtime, bool):
                raise HTTPException(
                    status_code=400,
                    detail="refresh_runtime は真偽値で指定してください",
                )

            if not provider or not model:
                raise HTTPException(
                    status_code=400,
                    detail="provider と model は必須です",
                )

            selected_managed_runtime = (
                managed_local_runtime_for_model(
                    server.config,
                    model,
                )
                if provider == "openai_compatible_local"
                else None
            )
            freetoken_selected = selected_managed_runtime == "freetoken"
            strata_selected = selected_managed_runtime == "strata"
            freetoken_settings: dict[str, Any] = {}
            strata_response_mode: str | None = None

            if strata_selected:
                if body.get("llama_cpp") not in (None, {}) or body.get("runtime_settings") not in (None, {}):
                    raise HTTPException(status_code=400, detail="Strataではllama.cpp設定を使用できません")
                llama_model_profile = None
                llama_profile_selected = False
                llama_cpp_settings = {}
                from src.service_manager import validate_openai_compatible_local_launch_selection

                try:
                    validate_openai_compatible_local_launch_selection(server.config, provider=provider, model=model)
                except (OSError, ValueError) as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
            elif freetoken_selected:
                forbidden_llama_input = bool(
                    body.get("llama_cpp") not in (None, {})
                    or body.get("runtime_settings") not in (None, {})
                    or isinstance(body.get("runtime"), dict)
                    or any(key in body for key in _LLAMA_CPP_SETTING_KEYS)
                )
                if forbidden_llama_input:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "FreeTokenではllama_cpp runtime設定を指定できません。"
                            "freetokenオブジェクトを使用してください"
                        ),
                    )
                if (
                    isinstance(base_url, str)
                    and base_url.strip()
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "FreeTokenの接続先はfreetoken.host/portで"
                            "指定してください"
                        ),
                    )
                if body.get("reasoning_effort") not in (None, ""):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "FreeTokenではllama.cpp reasoning_effortを"
                            "指定できません"
                        ),
                    )
                freetoken_settings = (
                    _normalize_freetoken_request_settings(body)
                )
                llama_model_profile = None
                llama_profile_selected = False
                llama_cpp_settings = {}
            else:
                llama_model_profile = (
                    llama_cpp_model_profile(model)
                    if provider == "openai_compatible_local"
                    else None
                )
                llama_profile_selected = llama_model_profile is not None
                llama_cpp_settings = (
                    _normalize_llama_cpp_request_settings(body)
                    if provider == "openai_compatible_local"
                    else {}
                )
                # ``local-model`` is the explicit external OpenAI-compatible
                # sentinel.  Ignore nested llama.cpp controls for it so a stale
                # path/alias cannot replace the operator-provided base URL.
                if (
                    provider == "openai_compatible_local"
                    and model.casefold() == "local-model"
                ):
                    llama_cpp_settings = {}

            requested_reasoning_effort = body.get("reasoning_effort")
            if strata_selected and requested_reasoning_effort not in (None, ""):
                if not isinstance(requested_reasoning_effort, str):
                    raise HTTPException(
                        status_code=400,
                        detail="Strataのresponse modeはfast / thinkingから選択してください",
                    )
                from ...llm.provider_mode_adapters import FAST_THINKING_MODE_OPTIONS

                strata_response_mode = requested_reasoning_effort.strip().lower()
                if strata_response_mode not in FAST_THINKING_MODE_OPTIONS:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Strataのresponse modeは "
                            f"{', '.join(FAST_THINKING_MODE_OPTIONS)} から選択してください"
                        ),
                    )
            if (
                provider == "openai_compatible_local"
                and not freetoken_selected
                and not strata_selected
                and isinstance(requested_reasoning_effort, str)
            ):
                effort_metadata = llama_cpp_reasoning_effort_metadata(model)
                normalized_effort = requested_reasoning_effort.strip().lower()
                if effort_metadata and normalized_effort not in effort_metadata["options"]:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "選択モデルの推論effortは "
                            f"{', '.join(effort_metadata['options'])} から選択してください"
                        ),
                    )
            deployment = resolve_llm_deployment(server.config)
            persist_user_config = not (
                deployment is not None and deployment.fixed
            )
            try:
                preflight_deployment(
                    server.config,
                    provider=provider,
                    model=model,
                    base_url=base_url if isinstance(base_url, str) else None,
                )
            except DeploymentMismatchError as exc:
                # Keep the error structural and secret-free.  The request is
                # rejected before config persistence or client creation.
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            if freetoken_selected:
                try:
                    runtime_state = await asyncio.to_thread(
                        local_runtime_manager.status,
                        model,
                        "freetoken",
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=str(exc),
                    ) from exc
                except Exception:
                    logger.exception(
                        "Failed to inspect FreeToken runtime before engine switch"
                    )
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to inspect FreeToken runtime",
                    ) from None
                if not bool(runtime_state.get("prepared")):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "FreeToken runtime/model is not prepared. "
                            "Prepare the managed runtime before switching."
                        ),
                    )

            # Process ownership, rather than the presence of a known profile,
            # is the only lifecycle gate.  Resolve it against a read-only
            # overlay so a request carrying ``auto_start=false`` is treated as
            # operator-owned before any managed validation/start/stop hook can
            # run.
            next_runtime_ownership = (
                _selection_runtime_ownership(
                    server.config,
                    provider,
                    model,
                    llama_cpp_settings=llama_cpp_settings,
                    freetoken_settings=freetoken_settings,
                )
                if provider == "openai_compatible_local"
                else None
            )
            next_managed_runtime = bool(
                next_runtime_ownership is not None
                and next_runtime_ownership.managed_runtime
            )
            llama_cpp_runtime_changed = (
                _llama_cpp_runtime_settings_changed(
                    server.config,
                    model=model,
                    settings=llama_cpp_settings,
                )
                if (
                    provider == "openai_compatible_local"
                    and not freetoken_selected
                and not strata_selected
                    and next_managed_runtime
                )
                else False
            )
            freetoken_runtime_changed = (
                _freetoken_runtime_settings_changed(
                    server.config,
                    model=model,
                    settings=freetoken_settings,
                )
                if freetoken_selected
                else False
            )
            local_runtime_changed = (
                llama_cpp_runtime_changed
                or freetoken_runtime_changed
            )

            if (
                deployment is None
                and Features.is_enterprise()
                and provider == "sglang"
            ):
                expected_model = str(
                    os.getenv("SGLANG_MODEL")
                    or server.config.get("sglang.model")
                    or server.config.get("llm_model")
                    or ""
                ).strip()
                if expected_model and model != expected_model:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "EnterpriseのSGLangは起動済みサーバーのモデルだけを"
                            f"使用できます: {expected_model}"
                        ),
                    )
                expected_base_url = str(os.getenv("SGLANG_BASE_URL") or "").strip().rstrip("/")
                if (
                    expected_base_url
                    and isinstance(base_url, str)
                    and base_url.strip().rstrip("/") != expected_base_url
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="EnterpriseのSGLang接続先はデプロイ設定で固定されています",
                    )
            if provider == "kimi":
                supplied_key = str(body.get("api_key") or "").strip()
                configured_key = str(server.config.get("kimi_api_key", "") or "").strip()
                if not supplied_key and not configured_key and not os.getenv("MOONSHOT_API_KEY"):
                    raise HTTPException(
                        status_code=400,
                        detail="Kimi APIキーを設定してください",
                    )
            if provider == "deepseek":
                supplied_key = str(body.get("api_key") or "").strip()
                configured_key = str(
                    server.config.get("deepseek_api_key", "") or ""
                ).strip()
                if (
                    not supplied_key
                    and not configured_key
                    and not os.getenv("DEEPSEEK_API_KEY")
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="DeepSeek APIキーを設定してください",
                    )
            if provider == "deepinfra":
                supplied_key = str(body.get("api_key") or "").strip()
                configured_key = str(
                    server.config.get("deepinfra_api_key", "") or ""
                ).strip()
                if (
                    not supplied_key
                    and not configured_key
                    and not os.getenv("DEEPINFRA_TOKEN")
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="DeepInfra APIキーを設定してください",
                    )
            if provider == "routing-profile" and model != "free-team":
                raise HTTPException(
                    status_code=400,
                    detail="未対応のルーティングプロファイルです",
                )
            if (
                provider == "routing-profile"
                and model == "free-team"
                and agent_team_orchestration_mode(server.config) == "director"
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Directorモードでは無料Teamを選択できません",
                )
            if (
                provider == "routing-profile"
                and model == "free-team"
                and server.config.get("routing_profiles.free-team.enabled", True)
                is False
            ):
                raise HTTPException(
                    status_code=409,
                    detail="無料Teamを有効にしてから選択してください",
                )

            previous_provider = str(server.config.get("llm_provider", "") or "")
            previous_model = str(server.config.get("llm_model", "") or "")
            if (
                refresh_runtime
                and strata_selected
                and previous_provider.strip().lower() == "openai_compatible_local"
                and previous_model == model
            ):
                local_runtime_changed = True
            previous_runtime_ownership = (
                provider_runtime_ownership(
                    "openai_compatible_local",
                    server.config,
                    model=previous_model,
                )
                if previous_provider.strip().lower() == "openai_compatible_local"
                else None
            )
            if (
                provider == "openai_compatible_local"
                and not freetoken_selected
                and not strata_selected
                and llama_cpp_settings
            ):
                # llama_cpp.host/port are canonical for an explicit manual or
                # managed runtime request; derive the client URL before
                # validation and persistence so a stale base_url cannot target
                # another listener.  ``auto_start=false`` remains operator
                # owned but must still round-trip its explicit endpoint.
                from src.service_manager import _llama_cpp_base_url

                base_url = _llama_cpp_base_url(
                    server.config,
                    model=model,
                    overrides=llama_cpp_settings,
                )
            if (
                provider == "openai_compatible_local"
                and next_managed_runtime
                and not freetoken_selected
                and not strata_selected
                and not (deployment is not None and deployment.fixed)
            ):
                if llama_profile_selected and not llama_cpp_settings:
                    from src.service_manager import _llama_cpp_base_url

                    base_url = _llama_cpp_base_url(
                        server.config,
                        model=model,
                        overrides=llama_cpp_settings,
                    )
                from src.service_manager import (
                    validate_openai_compatible_local_launch_selection,
                )

                try:
                    validate_openai_compatible_local_launch_selection(
                        server.config,
                        provider=provider,
                        model=model,
                        base_url=base_url if isinstance(base_url, str) else None,
                        llama_cpp_settings=llama_cpp_settings or None,
                        force_restart=llama_cpp_runtime_changed,
                    )
                except Exception as exc:
                    raise HTTPException(status_code=400, detail=str(exc))

            config_changes: dict[str, Any] = {}
            if strata_response_mode is not None:
                # This is a request-scoped response mode for the shared local
                # client.  It is removed before persistence so it cannot leak
                # into llama.cpp reasoning-effort settings; the final
                # ``llm_runtime_mode`` is persisted after client creation.
                config_changes["runtime.target_reasoning_effort"] = strata_response_mode

            # A profile-owned GGUF/alias/context must not survive a switch to
            # another profile (or to an external local-model).  Preserve any
            # explicitly supplied request values; absent keys are cleared so
            # the generic runtime resolves the new profile defaults.
            previous_profile = llama_cpp_model_profile(previous_model)
            # A user-defined GGUF selection has no registry profile, but it
            # is still runtime-managed when its nested settings carry a path
            # or served alias.  Resolve that ownership from the generic
            # runtime helpers so profile transitions clean up custom values
            # just like Muse/Qwen transitions.
            previous_runtime_managed = bool(
                previous_runtime_ownership is not None
                and previous_runtime_ownership.managed_runtime
            )
            previous_local_settings = server.config.get(
                "openai_compatible_local", {}
            )
            previous_llama_settings = (
                previous_local_settings.get("llama_cpp", {})
                if isinstance(previous_local_settings, dict)
                else {}
            )
            previous_llama_runtime_configured = bool(
                isinstance(previous_llama_settings, dict)
                and any(
                    value not in (None, "", [], {})
                    for value in previous_llama_settings.values()
                )
            )
            next_profile = (
                llama_cpp_model_profile(model)
                if (
                    provider == "openai_compatible_local"
                    and not freetoken_selected
                and not strata_selected
                )
                else None
            )
            previous_selection_id = str(
                previous_profile.get("id") if previous_profile else previous_model
            ).casefold()
            next_selection_id = str(
                next_profile.get("id") if next_profile else model
            ).casefold()
            profile_changed = bool(
                previous_model
                and previous_selection_id != next_selection_id
                and (
                    previous_runtime_managed
                    or (next_profile is not None and next_managed_runtime)
                )
            )
            if (
                persist_user_config
                and profile_changed
                and provider == "openai_compatible_local"
                and next_managed_runtime
                and not freetoken_selected
                and not strata_selected
            ):
                from src.service_manager._local_llm_servers import (
                    _PROFILE_RUNTIME_SETTING_KEYS,
                )

                # Keep the route tolerant of older service-manager builds
                # while ensuring MTP state cannot leak between profiles.
                profile_runtime_keys = tuple(
                    dict.fromkeys(
                        (*_PROFILE_RUNTIME_SETTING_KEYS, "mtp_enabled")
                    )
                )
                for profile_key in profile_runtime_keys:
                    if profile_key not in llama_cpp_settings:
                        config_changes[
                            f"openai_compatible_local.llama_cpp.{profile_key}"
                        ] = None

            # An unprofiled local-model/custom endpoint is external unless
            # the request supplies an explicit runtime configuration.  Do not
            # let a previous managed or stale llama.cpp mapping silently
            # follow that endpoint (especially its model path/alias).  The
            # explicit-settings case remains available for advanced manual
            # llama-server users and is persisted below.
            if (
                persist_user_config
                and provider == "openai_compatible_local"
                and not freetoken_selected
                and not strata_selected
                and not llama_cpp_settings
                and next_profile is None
                and previous_llama_runtime_configured
                and previous_model.casefold() != model.casefold()
            ):
                for runtime_key in (
                    "executable",
                    "model_path",
                    "model_root",
                    "model_alias",
                    "context_size",
                    "gpu_layers",
                    "extra_args",
                    "auto_start",
                    "readiness_timeout",
                    "reasoning_effort",
                    "mtp_enabled",
                ):
                    config_changes[
                        f"openai_compatible_local.llama_cpp.{runtime_key}"
                    ] = None

            def _apply_config(key: str, next_value: Any) -> None:
                # Deployment overrides are runtime-only.  Do not rewrite the
                # persisted DB selection merely because the UI confirms the
                # fixed effective model; it remains visible in diagnostics.
                if not persist_user_config:
                    return
                config_changes[key] = next_value

            # configを更新して永続化する
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
            if provider == "sglang":
                _apply_config("sglang.model", model)
            elif provider == "ollama":
                _apply_config("ollama.model", model)
            elif provider == "openai_compatible_local":
                _apply_config("openai_compatible_local.model", model)
                if freetoken_selected:
                    for setting_key, setting_value in freetoken_settings.items():
                        _apply_config(
                            f"openai_compatible_local.freetoken.{setting_key}",
                            setting_value,
                        )
                elif next_managed_runtime:
                    for setting_key, setting_value in llama_cpp_settings.items():
                        _apply_config(
                            f"openai_compatible_local.llama_cpp.{setting_key}",
                            setting_value,
                        )
                elif llama_cpp_settings:
                    # ``auto_start=false`` is an intentional operator-owned
                    # manual runtime, but its explicit settings still need to
                    # be persisted for the next launch/config round-trip.
                    for setting_key, setting_value in llama_cpp_settings.items():
                        _apply_config(
                            f"openai_compatible_local.llama_cpp.{setting_key}",
                            setting_value,
                        )
                elif (
                    "auto_start" in llama_cpp_settings
                    and llama_cpp_settings["auto_start"] is False
                ):
                    # An explicit false is the operator-ownership opt-out.  It
                    # is safe (and important) to persist that one setting so a
                    # later request cannot accidentally reclassify this route
                    # as AoiTalk-managed.  Other runtime controls remain
                    # untouched for external profile switches.
                    _apply_config(
                        "openai_compatible_local.llama_cpp.auto_start",
                        False,
                    )
            elif provider == "openrouter":
                _apply_config("openrouter.model", model)
            elif provider == "codex-cli":
                _apply_config("codex_cli.model", model)
            elif provider == "claude-cli":
                _apply_config("claude_cli.model", model)
            elif provider == "antigravity-cli":
                _apply_config("antigravity_cli.model", model)
            elif provider == "grok-cli":
                _apply_config("grok_cli.model", model)
            elif provider == "gemini":
                _apply_config("gemini.model", model)
            elif provider == "openai":
                _apply_config("openai.model", model)
            elif provider == "kimi":
                _apply_config("kimi.model", model)
            elif provider == "deepseek":
                _apply_config("deepseek.model", model)
            elif provider == "deepinfra":
                _apply_config("deepinfra.model", model)

            if (
                provider == "openai_compatible_local"
                and not freetoken_selected
                and not strata_selected
                and (
                    bool(llama_cpp_settings)
                    or (next_managed_runtime and llama_profile_selected)
                )
                and isinstance(base_url, str)
                and base_url.strip()
            ):
                # The nested runtime's host/port is authoritative.  This also
                # makes the client and readiness probe share one endpoint.
                _apply_config("openai_compatible_local.base_url", base_url)
            elif isinstance(base_url, str) and base_url.strip():
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
            elif (
                provider == "openai_compatible_local"
                and next_managed_runtime
                and not freetoken_selected
                and not strata_selected
            ):
                from src.llm.openai_compatible_local_profiles import (
                    local_server_profile_for_model,
                )

                profile = local_server_profile_for_model(model)
                if profile:
                    _apply_config(
                        "openai_compatible_local.base_url",
                        profile["base_url"],
                    )

            api_key = body.get("api_key")
            if isinstance(api_key, str) and api_key.strip():
                if provider == "openrouter":
                    _apply_config("openrouter_api_key", api_key.strip())
                elif provider == "kimi":
                    _apply_config("kimi_api_key", api_key.strip())
                elif provider == "deepseek":
                    _apply_config("deepseek_api_key", api_key.strip())
                elif provider == "deepinfra":
                    _apply_config("deepinfra_api_key", api_key.strip())
                elif provider == "ollama":
                    _apply_config("ollama.api_key", api_key.strip())
                elif provider == "openai_compatible_local":
                    _apply_config(
                        "openai_compatible_local.api_key",
                        api_key.strip(),
                    )
                elif provider == "sglang":
                    _apply_config("sglang_api_key", api_key.strip())

            if "enable_tools" in body and provider in {
                "ollama",
                "openai_compatible_local",
            }:
                _apply_config(f"{provider}.enable_tools", bool(body["enable_tools"]))

            if (
                "enable_response_format" in body
                and provider == "openai_compatible_local"
            ):
                _apply_config(
                    "openai_compatible_local.enable_response_format",
                    bool(body["enable_response_format"]),
                )

            if "enable_extra_body" in body and provider == "openai_compatible_local":
                _apply_config(
                    "openai_compatible_local.enable_extra_body",
                    bool(body["enable_extra_body"]),
                )

            reasoning_effort = body.get("reasoning_effort")
            if isinstance(reasoning_effort, str):
                effort = reasoning_effort.strip()
                if effort and provider == "codex-cli":
                    _apply_config("codex_cli.reasoning_effort", effort)
                elif effort and provider == "claude-cli":
                    _apply_config("claude_cli.reasoning_effort", effort)
                elif effort and provider == "kimi" and effort == "max":
                    _apply_config("kimi.reasoning_effort", "max")
                elif effort and provider == "deepseek":
                    if effort not in {"none", "high", "max"}:
                        raise HTTPException(
                            status_code=400,
                            detail="DeepSeekの推論モードは none / high / max から選択してください",
                        )
                    _apply_config("deepseek.reasoning_effort", effort)
                elif effort and provider == "deepinfra":
                    if effort not in {"none", "low", "medium", "high"}:
                        raise HTTPException(
                            status_code=400,
                            detail="DeepInfraの推論モードは none / low / medium / high から選択してください",
                        )
                    _apply_config("deepinfra.reasoning_effort", effort)
                elif (
                    effort
                    and provider == "openai_compatible_local"
                    and not freetoken_selected
                and not strata_selected
                ):
                    metadata = llama_cpp_reasoning_effort_metadata(model)
                    if metadata is not None:
                        if effort not in metadata["options"]:
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    "選択モデルの推論effortは "
                                    f"{', '.join(metadata['options'])} から選択してください"
                                ),
                            )
                        _apply_config(
                            "openai_compatible_local.llama_cpp.reasoning_effort",
                            effort,
                        )

            from src.service_manager import (
                should_resolve_llama_cpp_runtime_for_engine_switch,
            )

            if (
                persist_user_config
                and provider == "openai_compatible_local"
                # Persist registered profile runtime state regardless of
                # process ownership.  ``auto_start=false`` intentionally
                # selects an operator-owned endpoint, but its per-profile
                # settings must still round-trip through the same resolver
                # and canonical dotted-key patch as a managed selection.
                and (next_managed_runtime or llama_profile_selected)
                and not freetoken_selected
                and not strata_selected
                and should_resolve_llama_cpp_runtime_for_engine_switch(
                    server.config,
                    model=model,
                    llama_cpp_settings=llama_cpp_settings or None,
                    next_profile=next_profile,
                    previous_runtime_managed=previous_runtime_managed,
                    profile_changed=profile_changed,
                )
            ):
                from src.service_manager import (
                    _llama_cpp_settings,
                    build_llama_cpp_profile_runtime_patch,
                )

                if profile_changed and previous_runtime_managed:
                    config_changes.update(
                        build_llama_cpp_profile_runtime_patch(
                            server.config,
                            model=previous_model,
                        )
                    )
                resolved_llama_settings = _llama_cpp_settings(
                    server.config,
                    model=model,
                    overrides=llama_cpp_settings or None,
                )
                for setting_key in (
                    "model_path",
                    "model_alias",
                    "context_size",
                    "extra_args",
                    "gpu_layers",
                    "auto_start",
                    "mtp_enabled",
                    "readiness_timeout",
                ):
                    if setting_key not in llama_cpp_settings:
                        # Keep an existing operator-owned selection
                        # operator-owned when switching profiles without an
                        # explicit auto_start request.  The resolver's
                        # profile default is still recorded in the target
                        # profile entry below, while the top-level value
                        # remains the user's explicit ownership choice.
                        if setting_key == "auto_start" and not next_managed_runtime:
                            continue
                        config_changes[
                            f"openai_compatible_local.llama_cpp.{setting_key}"
                        ] = resolved_llama_settings.get(setting_key)
                config_changes.update(
                    build_llama_cpp_profile_runtime_patch(
                        server.config,
                        model=model,
                        settings=resolved_llama_settings,
                    )
                )

            staged_config = _ConfigOverlay(server.config, config_changes)
            previous_local_config = _ConfigOverlay(
                server.config,
                {
                    "llm_provider": previous_provider,
                    "llm_model": previous_model,
                    "openai_compatible_local": copy.deepcopy(
                        server.config.get("openai_compatible_local", {}) or {}
                    ),
                },
            )
            local_switch_in_progress = False

            def _compensate_local_server_switch() -> None:
                nonlocal local_switch_in_progress
                if not local_switch_in_progress:
                    return
                local_switch_in_progress = False
                from ...services.llm_engine_switch_service import (
                    switch_managed_local_runtime,
                )

                try:
                    # Reverse the worker transition against the still-live
                    # persisted route. This scopes cleanup to the candidate
                    # process and restores the previous managed target.
                    switch_managed_local_runtime(
                        server.config,
                        previous_provider=provider,
                        previous_model=model,
                        next_provider=previous_provider,
                        next_model=previous_model,
                        next_config=previous_local_config,
                        force_restart=False,
                        stop_same_scope=bool(local_runtime_changed),
                        resolve_endpoints=False,
                    )
                except Exception:
                    logger.exception("Failed to restore the previous local LLM runtime")

            # A managed local candidate must be started before client
            # construction. When switching away to a cloud/external route,
            # defer the old local stop until after the replacement client and
            # config commit so a lease timeout leaves the committed route live.
            manage_before_client = bool(
                provider == "openai_compatible_local"
                and (next_managed_runtime or freetoken_selected)
                and not (deployment is not None and deployment.fixed)
            )
            if manage_before_client:
                # Mark the transition before entering the worker so a
                # candidate preflight/readiness failure can restore an old
                # same-scope worker as well as a model-changing worker.
                local_switch_in_progress = bool(
                    local_runtime_changed
                    or (
                        previous_runtime_ownership is not None
                        and previous_runtime_ownership.managed_runtime
                    )
                    or next_managed_runtime
                )
                try:
                    from ...services.llm_engine_switch_service import (
                        switch_managed_local_runtime,
                    )

                    transition = await asyncio.to_thread(
                        switch_managed_local_runtime,
                        server.config,
                        previous_provider=previous_provider,
                        previous_model=previous_model,
                        next_provider=provider,
                        next_model=model,
                        next_config=staged_config,
                        force_restart=local_runtime_changed,
                        rollback_on_error=False,
                        # Admission validates the owned listener; weights are
                        # observed by the existing runtime status polling.
                        # Generation still uses strict same-alias readiness.
                        wait_for_model_ready=selected_managed_runtime != "llama_cpp",
                    )
                    stopped_local_servers = int(transition.get("stopped") or 0)
                    started_local_server = bool(transition.get("started"))
                    local_switch_in_progress = bool(
                        stopped_local_servers or started_local_server
                    )
                    if stopped_local_servers:
                        logger.info(
                            "Stopped %s managed OpenAI-compatible local server "
                            "process(es) during engine switch",
                            stopped_local_servers,
                        )
                except Exception as exc:
                    if not getattr(exc, "_managed_local_runtime_restored", False):
                        await asyncio.to_thread(_compensate_local_server_switch)
                    else:
                        local_switch_in_progress = False
                    raise HTTPException(status_code=400, detail=str(exc))

            # 新しいLLMクライアントを生成して差し替え
            from ...llm.manager import create_llm_client

            try:
                # Build against the staged selection before committing anything.
                # Failed preflight/client creation therefore leaves DB, config,
                # and the active client untouched.
                new_client = create_llm_client(staged_config)
                next_mode_state = build_llm_mode_state(staged_config, client=new_client)
                # Known llama.cpp profiles may explicitly expose no response
                # mode (for example a text-only roleplay profile with no
                # verified reasoning/thinking contract).  Do not resurrect the
                # generic ``fast`` fallback here: that would persist and send
                # an unsupported mode after a model switch.  Unknown external
                # local-model profiles still return their legacy fast/thinking
                # state from build_llm_mode_state.
                next_runtime_mode = str(next_mode_state.get("mode") or "").strip()
                if (
                    next_mode_state.get("kind") == "response_mode"
                    and hasattr(new_client, "set_llm_mode")
                ):
                    new_client.set_llm_mode(next_runtime_mode)

                old_client = server._llm_client
                old_runtime_mode = server._current_llm_mode
                if persist_user_config:
                    config_changes["llm_runtime_mode"] = next_runtime_mode
                if strata_selected:
                    config_changes.pop("runtime.target_reasoning_effort", None)

                # The production setter assigns first and catches callback
                # failures, so activate the ready client before DB commit.
                # If persistence fails, restoring the client requires no DB
                # rollback and therefore cannot clobber concurrent settings.
                try:
                    server.set_llm_client(new_client)
                except Exception:
                    _restore_llm_client(server, old_client)
                    raise
                if persist_user_config:
                    try:
                        _persist_config_changes(server.config, config_changes)
                    except Exception:
                        _restore_llm_client(server, old_client)
                        server._current_llm_mode = old_runtime_mode
                        raise
            except Exception:
                await asyncio.to_thread(_compensate_local_server_switch)
                raise

            server._current_llm_mode = next_runtime_mode
            local_switch_in_progress = False

            if not manage_before_client and not (
                deployment is not None and deployment.fixed
            ):
                from ...service_manager import _LlamaCppGenerationLeaseTimeout
                from ...services.llm_engine_switch_service import (
                    switch_managed_local_runtime,
                )

                try:
                    transition_config = (
                        server.config if manage_before_client else previous_local_config
                    )
                    await asyncio.to_thread(
                        switch_managed_local_runtime,
                        transition_config,
                        previous_provider=previous_provider,
                        previous_model=previous_model,
                        next_provider=provider,
                        next_model=model,
                        next_config=staged_config,
                        force_restart=False,
                        resolve_endpoints=manage_before_client,
                        timeout_seconds=0 if not manage_before_client else None,
                        defer_on_timeout=not manage_before_client,
                    )
                except _LlamaCppGenerationLeaseTimeout as exc:
                    # The route/client/config commit is already authoritative;
                    # leave the old owned worker running until its consumers
                    # drain rather than turning a successful switch into a
                    # REST failure.
                    logger.warning(
                        "旧managed local runtimeの停止を延期しました: %s",
                        exc,
                    )

            logger.info(f"LLM engine switched to {provider}/{model}")

            # 変更を起こしたクライアント（または管理者）へ通知
            try:
                await broadcast_llm_state_change(
                    server,
                    request,
                    {
                        "type": "llm_engine_change",
                        "data": {"provider": provider, "model": model},
                    },
                )
                await broadcast_llm_state_change(
                    server,
                    request,
                    {
                        "type": "llm_mode_change",
                        "data": next_mode_state,
                    },
                )
            except Exception:
                # The replacement is already active and persisted.  Treat a
                # WebSocket notification outage as non-fatal to the REST API.
                logger.warning("Failed to broadcast LLM engine change", exc_info=True)

            opts = await asyncio.to_thread(_build_engine_list, server.config)
            label = next(
                (
                    o["label"]
                    for o in opts
                    if o["provider"] == provider and o["model"] == model
                ),
                f"{model} ({provider})",
            )
            response = {
                "success": True,
                "provider": provider,
                "model": model,
                "message": f"言語モデルを {label} に切り替えました",
            }
            if deployment is not None:
                response["deployment"] = deployment.metadata()
                response["effective_provider"] = deployment.effective_provider
                response["effective_model"] = deployment.effective_model
            if provider == "openai_compatible_local":
                try:
                    response["llama_cpp"] = _llama_cpp_model_root_payload(
                        server.config
                    )
                except ValueError:
                    pass
            return _attach_server_timing(
                JSONResponse(response),
                "aoi_llm_engine",
                started_at,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to switch LLM engine: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/ollama/status")
    async def get_ollama_status(_: None = Depends(require_auth)):
        """Get local Ollama daemon status."""
        return JSONResponse(server._ollama_model_manager.status())

    @app.get("/api/ollama/models")
    async def get_ollama_models(_: None = Depends(require_auth)):
        """List installed Ollama models."""
        return JSONResponse(server._ollama_model_manager.list_models())

    @app.delete("/api/ollama/models")
    async def delete_ollama_model(
        payload: OllamaModelPayload, _: None = Depends(require_auth)
    ):
        """Delete an installed Ollama model."""
        try:
            result = server._ollama_model_manager.delete_model(payload.model)
            return JSONResponse(result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("Failed to delete Ollama model: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/ollama/pulls")
    async def get_ollama_pulls(_: None = Depends(require_auth)):
        """List recent Ollama pull tasks."""
        return JSONResponse({"pulls": server._ollama_model_manager.list_pulls()})

    @app.post("/api/ollama/pull")
    async def start_ollama_pull(
        payload: OllamaPullPayload, _: None = Depends(require_auth)
    ):
        """Start downloading an Ollama model in the background."""
        try:
            task = server._ollama_model_manager.start_pull(payload.model)
            return JSONResponse(task)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("Failed to start Ollama pull: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/ollama/pull/{task_id}")
    async def get_ollama_pull(task_id: str, _: None = Depends(require_auth)):
        """Get an Ollama pull task status."""
        task = server._ollama_model_manager.get_pull(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="pull task not found")
        return JSONResponse(task)

    @app.get("/api/llm/execution-profiles")
    async def get_execution_profiles(_: None = Depends(require_auth)):
        return JSONResponse(execution_profile_envelope(server.config))

    @app.put("/api/llm/execution-profiles")
    async def put_execution_profiles(request: Request, _: None = Depends(require_auth)):
        await _require_global_config_admin(request)
        del request
        # Global Execution Profiles are no longer a routing source of truth.
        # Team-scoped profiles live on Agent Team config.  This leftover
        # endpoint does not mutate Main or persist a second canonical store.
        return JSONResponse(execution_profile_envelope(server.config))

    @app.post("/api/llm/execution-profiles/{profile_id}/activate")
    async def activate_execution_profile_route(
        profile_id: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        await _require_global_config_admin(request)
        del profile_id, request
        main_route = resolve_execution_main_route(server.config)
        return JSONResponse(
            {
                "success": True,
                "profile": None,
                "execution_profile": execution_profile_envelope(server.config),
                "effective_main": {
                    "provider": main_route.get("provider"),
                    "model": main_route.get("model"),
                    "effort": main_route.get("effort")
                    or main_route.get("reasoning_effort"),
                },
            }
        )

    @app.get("/api/llm/session-settings")
    async def get_session_llm_settings(
        request: Request,
        session_id: str = Query(..., min_length=1),
        _: None = Depends(require_auth),
    ):
        from ...memory.conversation_repository import ConversationRepository

        await _require_session_settings_access(
            request,
            session_id,
            require_write=False,
        )
        repo = ConversationRepository()
        row = await repo.get_session_by_id(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        from ...services.session_llm_runtime import restore_session_agent_team_registry
        from ...services.conversation_session_selection import read_session_llm_settings

        settings = read_session_llm_settings(row.context)
        restore_session_agent_team_registry(
            str(row.user_id),
            session_id,
            settings,
        )
        return JSONResponse(
            session_llm_settings_envelope(row.context, server.config)
        )

    @app.get("/api/llm/new-chat-defaults")
    async def get_new_chat_llm_defaults(
        request: Request,
        _: None = Depends(require_auth),
    ):
        from ...services.user_llm_preference_service import get_user_last_used_main_route

        last_used_route: dict[str, str] = {}
        user_id = await _resolve_request_user_id(request)
        if user_id:
            try:
                last_used_route = await get_user_last_used_main_route(user_id)
            except Exception:
                last_used_route = {}
        return JSONResponse(
            new_chat_llm_defaults_envelope(server.config, last_used_route)
        )

    @app.put("/api/llm/new-chat-defaults")
    async def put_new_chat_llm_defaults(
        request: Request,
        _: None = Depends(require_auth),
    ):
        from ...services.user_llm_preference_service import (
            get_user_last_used_main_route,
            record_user_last_used_main_route,
        )

        last_used_route: dict[str, str] = {}
        user_id = await _resolve_request_user_id(request)
        if user_id:
            try:
                body = await request.json()
            except Exception:
                body = {}
            route = body.get("main_route") if isinstance(body, dict) else {}
            updated_at = body.get("updated_at") if isinstance(body, dict) else None
            if updated_at is None and isinstance(route, dict):
                updated_at = route.get("updated_at")
            try:
                await record_user_last_used_main_route(
                    user_id,
                    route,
                    updated_at=updated_at,
                )
                last_used_route = await get_user_last_used_main_route(user_id)
            except Exception:
                last_used_route = {}
        return JSONResponse(
            new_chat_llm_defaults_envelope(server.config, last_used_route)
        )

    @app.put("/api/llm/session-settings")
    async def put_session_llm_settings(
        request: Request,
        _: None = Depends(require_auth),
    ):
        from ...memory.conversation_repository import ConversationRepository
        from ...memory.database import get_database_manager

        body = await request.json()
        session_id = str(body.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        await _require_session_settings_access(
            request,
            session_id,
            require_write=True,
        )
        patch = {
            key: value
            for key, value in body.items()
            if key not in {"session_id"}
        }
        if isinstance(patch.get("settings"), dict):
            patch = patch["settings"]
        db_session = await get_database_manager().get_session()
        try:
            repo = ConversationRepository(db_session)
            row = await repo.get_session_by_id(session_id, for_update=True)
            if row is None:
                raise HTTPException(status_code=404, detail="Session not found")
            previous_session_settings = read_session_llm_settings(row.context)
            settings, warnings = validate_session_llm_settings(
                patch if isinstance(patch, dict) else {},
                context=row.context,
                config=server.config,
            )
            invalid_effort_warnings = [
                warning
                for warning in warnings
                if str(warning).startswith("Invalid reasoning effort for ")
            ]
            if invalid_effort_warnings:
                raise HTTPException(
                    status_code=400,
                    detail=invalid_effort_warnings[0],
                )
            merged_context = merge_session_llm_settings(row.context, settings)
            updated = await repo.update_session(
                session_id,
                touch_activity=False,
                context=merged_context,
            )
            if not updated:
                raise HTTPException(status_code=404, detail="Session not found")
        finally:
            await db_session.close()

        # Retire this session's response-model cache before touching a managed
        # local process.  The callback releases only idle consumer retention;
        # active generation/title work remains protected until its normal
        # terminal path.  This must run before the switch so an idle old
        # consumer cannot make an LFM -> Qwen transition defer indefinitely.
        if "main_route" in patch:
            retire_session_cache = getattr(
                server,
                "on_session_llm_route_change",
                None,
            )
            if callable(retire_session_cache):
                try:
                    from ...services.session_llm_generation import (
                        retire_session_response_model_cache,
                    )

                    retirement = await retire_session_response_model_cache(
                        retire_session_cache,
                        session_id,
                    )
                    deferred = retirement["deferred"]
                    errors = retirement["errors"]
                    if deferred:
                        warnings.append(
                            "このsessionの旧response-model clientは、"
                            f"active work終了後にconsumerを解放します ({deferred})"
                        )
                    if errors:
                        warnings.append(
                            "response-model consumerの解放を確認できませんでした。"
                            "次の送信時に再検証します"
                        )
                except Exception as exc:
                    # Persisted settings remain usable, but do not claim that
                    # the old local consumer was released when the owner-side
                    # retire hook failed.
                    warnings.append(
                        "response-model clientのretireを延期しました: "
                        f"{type(exc).__name__}"
                    )

        # Session route changes are persisted above before touching a managed
        # local process. This keeps the row lock short even when a cold model
        # takes a long time to start. Session routes are overlays, so only an
        # explicit previous local route is eligible for stop; the global Main
        # runtime remains owned by its own route and consumers.
        if "main_route" in patch:
            previous_route = session_main_route_override_for_binding(
                previous_session_settings
            ) or {}
            next_route = session_main_route_override_for_binding(
                read_session_llm_settings(merged_context)
            ) or {}
            previous_provider = str(previous_route.get("provider") or "").strip().lower()
            previous_model = str(previous_route.get("model") or "").strip()
            next_provider = str(next_route.get("provider") or "").strip().lower()
            next_model = str(next_route.get("model") or "").strip()
            if not next_provider or not next_model:
                # Clearing a session override returns to the global Main
                # route. Resolve that fallback before deciding whether the
                # previous explicit local runtime is still needed; otherwise
                # clearing an override for the same local model would stop
                # the global worker unnecessarily.
                global_route = resolve_execution_main_route(server.config)
                next_provider = str(global_route.get("provider") or "").strip().lower()
                next_model = str(global_route.get("model") or "").strip()
            if previous_provider == "openai_compatible_local" or (
                next_provider == "openai_compatible_local"
                and next_model
            ):
                try:
                    from ...llm.manager import TargetConfig
                    from ...services.llm_engine_switch_service import (
                        start_session_route_warmup,
                        switch_managed_local_runtime,
                    )

                    next_config = TargetConfig(
                        server.config,
                        {
                            "llm_provider": next_provider,
                            "llm_model": next_model,
                            "openai_compatible_local.model": next_model,
                        },
                    )
                    switch_kwargs = dict(
                        previous_provider=previous_provider,
                        previous_model=previous_model,
                        next_provider=next_provider,
                        next_model=next_model,
                        next_config=next_config,
                        force_restart=False,
                        timeout_seconds=(
                            0
                            if next_provider != "openai_compatible_local"
                            else None
                        ),
                        defer_on_timeout=next_provider != "openai_compatible_local",
                    )
                    if next_provider == "openai_compatible_local" and next_model:
                        # Do not hold this save until a cold model is ready:
                        # the WebUI queues the next save of this session behind
                        # it, which kept a newer selection from cancelling the
                        # obsolete warm-up.
                        start_session_route_warmup(server.config, **switch_kwargs)
                    else:
                        # Leaving a local route only stops it; the stop also
                        # cancels a still-loading warm-up of that scope.
                        await asyncio.to_thread(
                            switch_managed_local_runtime,
                            server.config,
                            supersedable=True,
                            **switch_kwargs,
                        )
                except Exception as exc:
                    # Settings are already durable and the next session turn
                    # has its own strict ensure path. Surface a warning while
                    # keeping the route selection usable for retry/recovery.
                    warnings.append(
                        "ローカルLLM runtimeの切替を延期しました。"
                        f"次の送信時に再試行します: {exc}"
                    )
        if "main_route" in patch:
            main_route = (
                settings.get("main_route")
                if isinstance(settings.get("main_route"), dict)
                else {}
            )
            # Admins and shared-session participants may write another user's
            # session, so last-used follows whoever made the selection.
            actor_id = await _resolve_request_user_id(request)
            owner_id = actor_id or str(row.user_id or "").strip()
            if owner_id:
                try:
                    from ...services.user_llm_preference_service import (
                        has_explicit_last_used_route,
                        record_user_last_used_main_route,
                    )

                    if has_explicit_last_used_route(main_route):
                        await record_user_last_used_main_route(owner_id, main_route)
                except Exception:
                    pass
        restore_session_agent_team_registry(
            str(row.user_id or ""),
            session_id,
            settings,
        )
        from ...services.session_llm_generation import invalidate_session_llm_client_cache

        invalidate_session_llm_client_cache(
            getattr(server, "_llm_client", None),
            session_id,
        )
        return JSONResponse(
            {
                "success": True,
                "warnings": warnings,
                **session_llm_settings_envelope(merged_context, server.config),
            }
        )
