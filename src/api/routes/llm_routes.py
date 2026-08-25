"""LLM モード/エンジン切替・モデルカタログ・Ollama モデル管理ルート (server.py から移設)"""

import asyncio
import copy
import json
import logging
import os
import re
import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ...services.llm_model_catalog import (
    build_engine_options as build_llm_engine_options,
    build_llm_mode_state,
    build_model_catalog as build_llm_model_catalog,
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
    session_llm_settings_envelope,
    validate_session_llm_settings,
)
from ...services.session_llm_runtime import restore_session_agent_team_registry
from ...services.provider_runtime_ownership import (
    ProviderRuntimeOwnership,
    provider_runtime_ownership,
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
    llama_cpp_model_profile,
    llama_cpp_reasoning_effort_metadata,
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
        "mtp_mode",
        "mtp_default_enabled",
        "mtp_artifact_filename",
        "mtp_compatibility",
        "mtp_ui_notice",
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
            if not isinstance(value, str):
                raise HTTPException(status_code=400, detail=f"llama_cpp.{key} は文字列で指定してください")
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
        try:
            result["gpu_layers"] = int(raw["gpu_layers"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="llama_cpp.gpu_layers は整数で指定してください")
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


def register_llm_routes(app: FastAPI, server: "WebChatServer") -> None:
    """LLM mode / models / engine / Ollama 管理ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

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

    async def _require_global_config_admin(request: Request) -> None:
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
    async def set_llm_mode(request: Request, _: None = Depends(require_auth)):
        """Set the current LLM response mode or reasoning effort."""
        await _require_global_config_admin(request)
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
    ):
        return build_llm_model_catalog(
            cfg,
            ollama_model_manager=server._ollama_model_manager,
            include_remote=include_remote,
            refresh_provider=refresh_provider,
            cached_catalog=cached_catalog,
        )

    def _build_engine_list(cfg):
        """APIキーの有無でフィルタリングした利用可能エンジン一覧を返す"""
        return build_llm_engine_options(
            cfg,
            ollama_model_manager=server._ollama_model_manager,
        )

    @app.get("/api/llm/models")
    async def get_llm_models(
        refresh: bool = Query(False),
        provider: Optional[str] = Query(None),
        _: None = Depends(require_auth),
    ):
        """Return provider-grouped model options for the settings screen."""
        cached_catalog = load_model_catalog_cache()
        catalog = _build_model_catalog(
            server.config,
            include_remote=refresh,
            refresh_provider=provider,
            cached_catalog=cached_catalog,
        )
        if refresh and provider:
            refreshed_provider = next(
                (item for item in catalog["providers"] if item["id"] == provider),
                None,
            )
            if refreshed_provider:
                next_cache = update_model_catalog_cache(
                    cached_catalog,
                    provider,
                    refreshed_provider.get("models") or [],
                )
                if next_cache != cached_catalog:
                    try:
                        save_model_catalog_cache(next_cache)
                        catalog = _build_model_catalog(
                            server.config,
                            include_remote=False,
                            refresh_provider=None,
                            cached_catalog=next_cache,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to save LLM model catalog cache: %s",
                            exc,
                        )
        return JSONResponse(catalog)

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
    async def get_llm_engine():
        """現在のLLMエンジン情報と利用可能エンジン一覧を返す"""
        provider = server.config.get("llm_provider", "openai")
        model = server.config.get("llm_model", "gpt-4o")
        deployment = resolve_llm_deployment(server.config)
        deployment_payload = deployment.metadata() if deployment else None
        response = {
            "provider": provider,
            "model": model,
            "persisted_provider": provider,
            "persisted_model": model,
            "available": _build_engine_list(server.config),
            "execution_profile": execution_profile_envelope(server.config),
            "effective_main": resolve_execution_main_route(server.config),
        }
        if deployment_payload is not None:
            response["deployment"] = deployment_payload
            response["effective_provider"] = deployment.effective_provider
            response["effective_model"] = deployment.effective_model
        return JSONResponse(response)

    @app.post("/api/llm/engine")
    async def set_llm_engine(request: Request, _: None = Depends(require_auth)):
        """Switch the active LLM engine (provider/model)."""
        await _require_global_config_admin(request)
        try:
            body = await request.json()
            provider = str(body.get("provider", "")).strip()
            model = str(body.get("model", "")).strip()
            base_url = body.get("base_url")
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
            if not provider or not model:
                raise HTTPException(
                    status_code=400, detail="provider と model は必須です"
                )
            requested_reasoning_effort = body.get("reasoning_effort")
            if provider == "openai_compatible_local" and isinstance(
                requested_reasoning_effort, str
            ):
                effort_metadata = llama_cpp_reasoning_effort_metadata(model)
                normalized_effort = requested_reasoning_effort.strip().lower()
                if effort_metadata and normalized_effort not in effort_metadata["options"]:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Qwen3.8の推論effortは "
                            f"{', '.join(effort_metadata['options'])} から選択してください"
                        ),
                    )
            deployment = resolve_llm_deployment(server.config)
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
                if provider == "openai_compatible_local" and next_managed_runtime
                else False
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
            previous_runtime_ownership = (
                provider_runtime_ownership(
                    "openai_compatible_local",
                    server.config,
                    model=previous_model,
                )
                if previous_provider.strip().lower() == "openai_compatible_local"
                else None
            )
            should_stop_previous_local_server = (
                _should_stop_previous_openai_compatible_local_server(
                    previous_provider,
                    previous_model,
                    provider,
                    model,
                    previous_ownership=previous_runtime_ownership,
                    next_ownership=next_runtime_ownership,
                )
            )
            if deployment is not None and deployment.fixed:
                # A fixed Enterprise backend owns its local server lifecycle;
                # never stop/start an operator-managed llama.cpp/Ollama process
                # as a side effect of selecting the already-effective model.
                should_stop_previous_local_server = False
            stopped_local_servers = 0

            if (
                provider == "openai_compatible_local"
                and next_managed_runtime
                and not (deployment is not None and deployment.fixed)
            ):
                if llama_cpp_settings or llama_profile_selected:
                    # llama_cpp.host/port are canonical for this runtime;
                    # derive the client URL before validation and persistence
                    # so a stale base_url cannot target another listener.
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
            next_profile = (
                llama_cpp_model_profile(model)
                if provider == "openai_compatible_local"
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
                profile_changed
                and provider == "openai_compatible_local"
                and next_managed_runtime
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

            def _apply_config(key: str, next_value: Any) -> None:
                # Deployment overrides are runtime-only.  Do not rewrite the
                # persisted DB selection merely because the UI confirms the
                # fixed effective model; it remains visible in diagnostics.
                if deployment is not None and deployment.fixed:
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
                if next_managed_runtime:
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
                and next_managed_runtime
                and (llama_cpp_settings or llama_profile_selected)
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
            elif provider == "openai_compatible_local" and next_managed_runtime:
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
                elif effort and provider == "openai_compatible_local":
                    metadata = llama_cpp_reasoning_effort_metadata(model)
                    if metadata is not None:
                        if effort not in metadata["options"]:
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    "Qwen3.8の推論effortは "
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
                provider == "openai_compatible_local"
                and next_managed_runtime
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
                ):
                    if setting_key not in llama_cpp_settings:
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
            local_switch_in_progress = (
                previous_provider == "openai_compatible_local"
                and provider == "openai_compatible_local"
                and next_managed_runtime
                and (should_stop_previous_local_server or llama_cpp_runtime_changed)
            )

            def _compensate_local_server_switch() -> None:
                nonlocal local_switch_in_progress
                if not local_switch_in_progress:
                    return
                local_switch_in_progress = False
                from src.service_manager import (
                    ensure_openai_compatible_local_server,
                    stop_owned_openai_compatible_local_servers_respecting_generation_leases,
                )

                try:
                    stop_owned_openai_compatible_local_servers_respecting_generation_leases()
                except Exception:
                    logger.exception(
                        "Failed to stop the replacement local LLM server"
                    )
                try:
                    ensure_openai_compatible_local_server(
                        previous_local_config,
                        raise_on_launch_error=False,
                        force_restart=False,
                    )
                except Exception:
                    logger.exception(
                        "Failed to restore the previous local LLM server"
                    )

            if (
                provider == "openai_compatible_local"
                and next_managed_runtime
                and not (deployment is not None and deployment.fixed)
            ):
                from src.service_manager import (
                    ensure_openai_compatible_local_server,
                    stop_owned_openai_compatible_local_servers_respecting_generation_leases,
                )

                try:
                    if should_stop_previous_local_server:
                        stopped_local_servers = await asyncio.to_thread(
                            stop_owned_openai_compatible_local_servers_respecting_generation_leases
                        )
                        if stopped_local_servers:
                            logger.info(
                                "Stopped %s managed OpenAI-compatible local server "
                                "process(es) before local model switch",
                                stopped_local_servers,
                            )

                    await asyncio.to_thread(
                        ensure_openai_compatible_local_server,
                        staged_config,
                        raise_on_launch_error=True,
                        force_restart=llama_cpp_runtime_changed,
                    )
                except Exception as exc:
                    await asyncio.to_thread(_compensate_local_server_switch)
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
                config_changes["llm_runtime_mode"] = next_runtime_mode

                # The production setter assigns first and catches callback
                # failures, so activate the ready client before DB commit.
                # If persistence fails, restoring the client requires no DB
                # rollback and therefore cannot clobber concurrent settings.
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
            except Exception:
                await asyncio.to_thread(_compensate_local_server_switch)
                raise

            server._current_llm_mode = next_runtime_mode
            local_switch_in_progress = False

            if (
                should_stop_previous_local_server
                and (
                    provider != "openai_compatible_local"
                    or not next_managed_runtime
                )
            ):
                from src.service_manager import (
                    _LlamaCppGenerationLeaseTimeout,
                    stop_owned_openai_compatible_local_servers_respecting_generation_leases,
                )

                try:
                    stopped_local_servers = await asyncio.to_thread(
                        stop_owned_openai_compatible_local_servers_respecting_generation_leases
                    )
                except _LlamaCppGenerationLeaseTimeout as exc:
                    # The new client and persisted route are already active at
                    # this point.  A lease timeout therefore cannot turn the
                    # completed switch into an HTTP failure: leave the old
                    # managed process running and report the new route as the
                    # successful, authoritative state.
                    logger.warning(
                        "旧managed llama.cppの停止を延期しました。"
                        "切替済みのclient/configを維持します: %s",
                        exc,
                    )
                    stopped_local_servers = 0
                if stopped_local_servers:
                    logger.info(
                        "Stopped %s managed OpenAI-compatible local server "
                        "process(es) after switching away",
                        stopped_local_servers,
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

            opts = _build_engine_list(server.config)
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
            return JSONResponse(response)
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
