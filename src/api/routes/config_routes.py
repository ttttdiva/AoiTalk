"""アプリ設定・音声状態・キャラクター系ルート (server.py から移設)"""

import asyncio
import copy
import logging
import math
import os
from ...services.browser_agent_models import browser_settings_payload, validate_browser_settings_update
import re
import sys
import time
from typing import TYPE_CHECKING, Any, Literal
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ...config_errors import (
    CharacterLookupError,
    CharacterNotFoundError,
    add_character_lookup_context,
    character_lookup_http_detail,
    character_lookup_http_status,
)
from ..router_helpers import cookie_auth_dependency
from ...features import Features
from ...services.character_service import canonicalize_character_slug
from .payloads import SettingsPayload
from ...services.runtime_policy_utils import MODEL_ROUTING_PROVIDERS
from ...services.model_roles_service import (
    ModelRolesPersistenceError,
    ModelRolesVersionConflict,
    model_roles_view,
    update_model_roles,
)
from ...llm.sglang_url import enforce_enterprise_sglang_model
from ...agent_harness.config import public_agent_harness_settings
from ...security.settings_public import (
    CLI_TRUSTED_WINDOWS_AGENT_IDS_SETTING_KEY,
    format_setting_log_value,
    is_admin_only_setting_key,
    mask_model_routing_classes,
    mask_secret_dict,
    public_setting_patch_payload,
)
from ...services.character_scope import (
    resolve_request_character_name,
    update_user_preferred_character,
)
from ...services.agent_root_lifecycle import AgentRootLifecycleError
from ...services.embedding_feature import (
    embedding_enabled,
    invalidate_runtime_embedding_cache,
)
from ...app_config_store import (
    load_stored_app_config_sync,
    update_app_config_keys_sync,
    OPENROUTER_ENABLE_TOOLS_KEY,
    OPENROUTER_ENABLE_TOOLS_SOURCE_PATH,
    OPENROUTER_ENABLE_TOOLS_USER_PROVENANCE,
)

# Import CharacterSwitchManager (server.py と同じフォールバック付き)
try:
    from ...tools.keyword.character_manager import CharacterSwitchManager
except ImportError:
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).parent.parent.parent))
    from tools.keyword.character_manager import CharacterSwitchManager

if TYPE_CHECKING:
    from ..server import WebChatServer

logger = logging.getLogger(__name__)


class MuseAllowedAgentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID


class MuseCanonicalPair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    revision_id: uuid.UUID


class MuseCanonicalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pair: MuseCanonicalPair
    expected: MuseCanonicalPair | None


class ModelRolesErrorResponse(BaseModel):
    detail: str


class ModelRolesLLMRouteSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["llm"]
    provider: str
    model: str
    effort: str | None


class ModelRolesPoolRouteSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["pool"]
    pool_id: str


ModelRolesRouteSchema = ModelRolesLLMRouteSchema | ModelRolesPoolRouteSchema


class ModelRolesChatSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fallbacks: list[ModelRolesRouteSchema] = Field(max_length=5)
    allow_paid_fallback: bool


class ModelRolesImageGenerationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: ModelRolesLLMRouteSchema | None


class ModelRolesRolesSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat: ModelRolesChatSchema
    image_generation: ModelRolesImageGenerationSchema


class ModelRolesViewSchema(BaseModel):
    version: int
    roles: ModelRolesRolesSchema
    billing_classes: dict[str, Literal["free", "subscription", "paid", "local"]]


_MODEL_ROLES_UPDATE_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["expected_version", "roles"],
                    "properties": {
                        "expected_version": {"type": "integer", "minimum": 0},
                        "roles": {
                            "$ref": "#/components/schemas/ModelRolesRolesSchema"
                        },
                    },
                }
            }
        },
    }
}

_MODEL_ROLES_AUTH_RESPONSES = {
    401: {"model": ModelRolesErrorResponse, "description": "Authentication required"},
}

_MUSE_REMOTE_TARGET_KEY = "remote_targets.muse"
_MUSE_REMOTE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,256}$")
_MUSE_REMOTE_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_MUSE_REMOTE_MAX_LIST_ITEMS = 64
_MUSE_REMOTE_MAX_STATE_ROOT_CHARS = 4096
_MUSE_REMOTE_MAX_RECONCILE_SECONDS = 600.0
_PERSONAL_DEADLINE_CHAT_DRAFT_KEY = "personal.deadline_chat_draft.enabled"
_PC_BRIDGE_REMOTE_ENABLED_KEY = "pc_bridge.remote_enabled"
_CLI_TRUSTED_WINDOWS_AGENT_IDS_MAX_ITEMS = 256
_MUSE_REMOTE_MAX_POLL_SECONDS = 60.0


def _validate_muse_remote_target(value: Any) -> dict[str, Any]:
    """Validate the strict, server-owned Muse remote target settings object."""

    if not isinstance(value, dict):
        raise ValueError("Invalid Muse remote target settings")

    allowed_fields = {
        "enabled",
        "egress_enabled",
        "authenticated",
        "credential_ref",
        "state_root",
        "vm_id",
        "node_id",
        "auth_fingerprint",
        "allowed_owner_ids",
        "allowed_agent_ids",
        "wait_seconds",
        "reconcile_timeout_seconds",
        "poll_interval_seconds",
        "chat_agent_id",
        "chat_agent_revision_id",
        "agent_revision_id",
        "revision_id",
    }
    if set(value) - allowed_fields:
        raise ValueError("Invalid Muse remote target settings")

    normalized: dict[str, Any] = {}
    for key in ("enabled", "egress_enabled", "authenticated"):
        if key not in value:
            continue
        item = value[key]
        if not isinstance(item, bool):
            raise ValueError("Invalid Muse remote target settings")
        normalized[key] = item

    for key in ("credential_ref", "vm_id", "node_id"):
        if key not in value:
            continue
        item = value[key]
        if not isinstance(item, str):
            raise ValueError("Invalid Muse remote target settings")
        item = item.strip()
        if not _MUSE_REMOTE_IDENTIFIER_RE.fullmatch(item):
            raise ValueError("Invalid Muse remote target settings")
        normalized[key] = item

    if "auth_fingerprint" in value:
        fingerprint = value["auth_fingerprint"]
        if not isinstance(fingerprint, str):
            raise ValueError("Invalid Muse remote target settings")
        fingerprint = fingerprint.strip()
        if not _MUSE_REMOTE_FINGERPRINT_RE.fullmatch(fingerprint):
            raise ValueError("Invalid Muse remote target settings")
        normalized["auth_fingerprint"] = fingerprint.lower()

    if "state_root" in value:
        state_root = value["state_root"]
        if not isinstance(state_root, str):
            raise ValueError("Invalid Muse remote target settings")
        state_root = state_root.strip()
        if (
            not state_root
            or len(state_root) > _MUSE_REMOTE_MAX_STATE_ROOT_CHARS
            or "\x00" in state_root
        ):
            raise ValueError("Invalid Muse remote target settings")
        from pathlib import Path

        try:
            root = Path(state_root)
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("Invalid Muse remote target settings")
            home = Path.home()
            if os.path.normcase(os.path.realpath(root)) == os.path.normcase(
                os.path.realpath(home)
            ):
                raise ValueError("Invalid Muse remote target settings")
        except (OSError, RuntimeError, TypeError):
            raise ValueError("Invalid Muse remote target settings") from None
        state_root = str(root)
        normalized["state_root"] = state_root

    for key in ("allowed_owner_ids", "allowed_agent_ids"):
        if key not in value:
            continue
        raw_ids = value[key]
        if not isinstance(raw_ids, list) or len(raw_ids) > _MUSE_REMOTE_MAX_LIST_ITEMS:
            raise ValueError("Invalid Muse remote target settings")
        ids: list[str] = []
        seen: set[str] = set()
        for raw_id in raw_ids:
            if not isinstance(raw_id, str):
                raise ValueError("Invalid Muse remote target settings")
            try:
                canonical_id = str(uuid.UUID(raw_id.strip()))
            except (ValueError, AttributeError):
                raise ValueError("Invalid Muse remote target settings") from None
            if canonical_id not in seen:
                seen.add(canonical_id)
                ids.append(canonical_id)
        normalized[key] = ids

    pin_keys = {"chat_agent_id", "chat_agent_revision_id", "agent_revision_id", "revision_id"}
    if pin_keys & value.keys():
        binding = _muse_canonical_binding(value)
        if binding is None or "chat_agent_revision_id" not in value:
            raise ValueError("Invalid Muse remote target settings")
        normalized["chat_agent_id"] = binding.agent_id
        normalized["chat_agent_revision_id"] = binding.agent_revision_id
        for key in ("agent_revision_id", "revision_id"):
            if key in value:
                normalized[key] = binding.agent_revision_id

    from ...services.muse_remote_execution import MAX_WAIT_SECONDS

    timing_bounds = {
        "wait_seconds": (0.0, float(MAX_WAIT_SECONDS), False),
        "reconcile_timeout_seconds": (0.0, _MUSE_REMOTE_MAX_RECONCILE_SECONDS, True),
        "poll_interval_seconds": (0.0, _MUSE_REMOTE_MAX_POLL_SECONDS, True),
    }
    for key, (minimum, maximum, exclusive_minimum) in timing_bounds.items():
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("Invalid Muse remote target settings")
        numeric = float(item)
        if not math.isfinite(numeric):
            raise ValueError("Invalid Muse remote target settings")
        if (numeric <= minimum if exclusive_minimum else numeric < minimum) or numeric > maximum:
            raise ValueError("Invalid Muse remote target settings")
        normalized[key] = numeric

    if normalized.get("enabled") is True:
        if (
            normalized.get("egress_enabled") is not True
            or normalized.get("authenticated") is not True
        ):
            raise ValueError("Invalid Muse remote target settings")
        if not all(
            normalized.get(key)
            for key in (
                "credential_ref",
                "state_root",
                "vm_id",
                "auth_fingerprint",
            )
        ):
            raise ValueError("Invalid Muse remote target settings")
        if not normalized.get("allowed_owner_ids") or not normalized.get(
            "allowed_agent_ids"
        ):
            raise ValueError("Invalid Muse remote target settings")

    return normalized


def _muse_canonical_binding(target: Any):
    from ...services.remote_llm_consumers import resolve_canonical_muse_binding
    return resolve_canonical_muse_binding({"remote_targets": {"muse": target}})


def _muse_canonical_public(target: Any) -> dict[str, Any]:
    binding = (_muse_canonical_binding(target)
               if isinstance(target, dict) and "chat_agent_revision_id" in target else None)
    return {"configured": binding is not None,
            "agent_id": binding.agent_id if binding else None,
            "revision_id": binding.agent_revision_id if binding else None}


def _read_stored_muse_target():
    # Read the parent to distinguish a missing key from JSON null, preserving the AAD path.
    from sqlalchemy import select
    from ...app_config_store import _db_deps, GLOBAL_CONFIG_KEY, APP_CONFIG_MISSING
    from ...security.field_crypto import decrypt_json_secret_leaves
    _, get_db, model = _db_deps()
    with get_db().get_sync_session() as session:
        parent = session.execute(select(model.value["remote_targets"]).where(
            model.key == GLOBAL_CONFIG_KEY)).scalar_one_or_none()
    if not isinstance(parent, dict) or "muse" not in parent:
        return APP_CONFIG_MISSING
    target = parent["muse"]
    wrapped = decrypt_json_secret_leaves({"remote_targets": {"muse": target}},
                                         aad_prefix="app_config_settings.value")
    return wrapped["remote_targets"]["muse"]


def _muse_remote_target_summary(value: Any) -> dict[str, Any]:
    """Expose only non-sensitive readiness assertions and allowlist counts."""

    target = value if isinstance(value, dict) else {}
    nonempty = bool(target)

    def _present(key: str) -> bool:
        item = target.get(key)
        return isinstance(item, str) and bool(item.strip())

    def _count(key: str) -> int:
        item = target.get(key)
        return len(item) if isinstance(item, list) else 0

    return {
        "configured": nonempty,
        "enabled": target.get("enabled") is True,
        "egress_enabled": target.get("egress_enabled") is True,
        "authenticated_configured": target.get("authenticated") is True,
        "credential_ref_present": _present("credential_ref"),
        "state_root_present": _present("state_root"),
        "vm_id_present": _present("vm_id"),
        "node_id_present": _present("node_id"),
        "auth_fingerprint_present": _present("auth_fingerprint"),
        "allowed_owner_count": _count("allowed_owner_ids"),
        "allowed_agent_count": _count("allowed_agent_ids"),
    }


def _muse_remote_target_profile_enabled() -> bool:
    """Fail closed when the current feature profile cannot use Muse remote."""

    try:
        return not Features.is_enterprise() and Features.remote_server_view()
    except Exception:
        return False


def _attach_server_timing(
    response: JSONResponse,
    name: str,
    started_at: float,
) -> JSONResponse:
    """Attach a stable, proxy-safe Server-Timing diagnostic to *response*."""

    duration_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)
    response.headers["Server-Timing"] = f"{name};dur={duration_ms:.1f}"
    return response


def _request_correlation_ids(request: Request | None) -> tuple[str | None, str | None]:
    """Read optional correlation headers without echoing their raw values."""

    if request is None:
        return None, None
    headers = request.headers
    return (
        headers.get("x-request-id") or headers.get("x-correlation-id"),
        headers.get("x-trace-id"),
    )


def _character_lookup_http_exception(
    exc: BaseException,
    *,
    request: Request | None = None,
    not_found_status: int = 404,
) -> HTTPException:
    """Map a character lookup failure to a secret-free HTTP exception."""

    if isinstance(exc, CharacterNotFoundError):
        return HTTPException(
            status_code=not_found_status,
            detail="Character not found",
        )
    request_id, trace_id = _request_correlation_ids(request)
    typed = add_character_lookup_context(
        exc,
        request_id=request_id,
        trace_id=trace_id,
    )
    return HTTPException(
        status_code=character_lookup_http_status(typed),
        detail=character_lookup_http_detail(typed),
    )


def _load_character_catalog(config: Any) -> tuple[list[str], list[dict[str, str]]]:
    """Load character names and slug options with one catalog lookup.

    ``Config.get_available_characters`` is a compatibility projection over
    ``get_available_character_options``.  Prefer that richer projection when
    available so the API does not issue the same database list query twice;
    lightweight configs that only implement the legacy method retain their
    existing fallback behavior.
    """

    option_loader = getattr(config, "get_available_character_options", None)
    if callable(option_loader):
        character_options = option_loader()
        if not isinstance(character_options, list):
            character_options = list(character_options or [])
        characters = sorted(
            {
                option["name"]
                for option in character_options
                if isinstance(option, dict) and option.get("name")
            }
        )
        return characters, character_options

    characters_loader = getattr(config, "get_available_characters", None)
    if not callable(characters_loader):
        return [], []
    characters = list(characters_loader())
    return characters, [{"slug": name, "name": name} for name in characters]


def _validate_harness_workspace_root(value: Any) -> str | None:
    """Return an existing absolute directory path, or None to clear."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ValueError("harness_execution.workspace_root must be a string path")
    raw = value.strip()
    if chr(0) in raw:
        raise ValueError("harness_execution.workspace_root is invalid")
    from pathlib import Path

    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("harness_execution.workspace_root must be an absolute path")
    if not path.is_dir():
        raise ValueError("harness_execution.workspace_root must be an existing directory")
    return str(path)


_EMBEDDING_MASTER_KEY = "search.embedding_enabled"
_EMBEDDING_RUNTIME_MODULE = "src.rag.embedding"


def _sync_embedding_master_runtime(value: Any) -> None:
    """Mirror a persisted embedding master switch onto an already-live wrapper.

    The embedding master flag is only the DB authority; the runtime observer
    reads that row itself.  This hook therefore never creates, imports, loads or
    starts the model and never awaits IPC: it only looks up the *existing*
    ``src.rag.embedding`` module in ``sys.modules`` (importing the RAG package
    can pull heavyweight optional dependencies), asks its passive
    ``peek_embedding_runtime()`` for a live wrapper, and calls the synchronous
    ``apply_master_enabled()`` counterpart that marks the gate and schedules the
    graceful drain.  Failure here is logged, never raised: the setting has
    already been persisted at this point.
    """
    try:
        module = sys.modules.get(_EMBEDDING_RUNTIME_MODULE)
        peek = getattr(module, "peek_embedding_runtime", None)
        runtime = peek() if callable(peek) else None
        apply_master_enabled = getattr(runtime, "apply_master_enabled", None)
        if callable(apply_master_enabled):
            apply_master_enabled(value)
    except Exception as exc:  # noqa: BLE001 - persistence already succeeded
        logger.warning(
            "Embedding master runtime sync failed: exception_type=%s",
            type(exc).__name__,
        )
    if value is True:
        # The legacy in-memory Docs queue pauses while the master is OFF. Wake
        # it on ON without importing Docs or awaiting indexing in the settings
        # response path.
        try:
            docs_module = sys.modules.get("src.rag.docs_index")
            resume = getattr(docs_module, "flush_pending_docs_reindex", None)
            if not callable(resume):
                return
            loop = asyncio.get_running_loop()
            pending = resume()
            try:
                loop.create_task(
                    pending,
                    name="aoitalk:resume-docs-reindex-after-embedding-on",
                )
            except Exception:
                pending.close()
                raise
        except Exception as exc:  # noqa: BLE001 - persistence already succeeded
            logger.warning(
                "Docs index resume after embedding ON failed: exception_type=%s",
                type(exc).__name__,
            )


def register_config_routes(app: FastAPI, server: "WebChatServer") -> None:
    """config / voice_status / characters / settings / character 切替ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    @app.get("/api/config")
    async def get_config(request: Request, _: None = Depends(require_auth)):
        """Get configuration"""
        # Handle both dict and Config object
        if hasattr(server.config, "config"):
            # Config object
            config_dict = server.config.config
            llm_model = config_dict.get("llm_model", "unknown")
            llm_provider = config_dict.get("llm_provider", "unknown")
            speech_config = config_dict.get("speech_recognition", {})
        else:
            # Plain dict
            llm_model = server.config.get("llm_model", "unknown")
            llm_provider = server.config.get("llm_provider", "unknown")
            speech_config = server.config.get("speech_recognition", {})

        # エージェントツール系の場合はプロバイダー名のみを表示
        # (antigravity-cli, codex, claude codeなどはモデル名ではなくツール名を表示)
        agent_tool_providers = ["antigravity-cli", "codex-cli", "claude-cli", "grok-cli"]
        if llm_provider in agent_tool_providers:
            # エージェントツール系の場合はモデル名をプロバイダー名に置き換え
            llm_model = llm_provider

        speech_engine = speech_config.get("current_engine", "unknown")
        speech_model = (
            speech_config.get("engines", {})
            .get(speech_engine, {})
            .get("model", "unknown")
        )

        # セッションIDを取得
        from ...utils.app_session import get_session_id

        session_id = get_session_id()
        character_name = await resolve_request_character_name(server, request)

        # Debug logging
        logger.info(
            f"API Config Response - LLM: {llm_model} ({llm_provider}), ASR: {speech_engine} ({speech_model})"
        )

        return JSONResponse(
            {
                "character_name": character_name,
                "max_history": server.manager.max_history,
                "llm_model": llm_model,
                "llm_provider": llm_provider,
                "asr_engine": speech_engine,
                "asr_model": speech_model,
                "session_id": session_id,
            }
        )

    @app.get("/api/voice_status")
    async def get_voice_status(_: None = Depends(require_auth)):
        """Get voice recognition status"""
        return JSONResponse(
            {
                "ready": server.voice_recognition_ready,
                "rms": server.current_rms,
                "recording": server.is_recording,
            }
        )

    @app.get("/api/characters")
    async def get_characters(
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Get list of available characters"""
        started_at = time.perf_counter()
        try:
            # Character catalog access ultimately performs synchronous DB work
            # through the legacy Config adapter. Run the complete projection in
            # a worker and derive legacy names from the same option list.
            characters, character_options = await asyncio.to_thread(
                _load_character_catalog,
                server.config,
            )
            current_character = await resolve_request_character_name(
                server,
                request,
                character_options=character_options,
            )
            if any(
                option.get("slug") == "project_manager"
                for option in character_options
                if isinstance(option, dict)
            ):
                current_character = canonicalize_character_slug(current_character)
            return _attach_server_timing(
                JSONResponse(
                    {
                        "characters": characters,
                        "character_options": character_options,
                        "current": current_character,
                    }
                ),
                "aoi_characters",
                started_at,
            )
        except CharacterLookupError as exc:
            logger.error(
                "Character list lookup failed: category=%s trace_id=%s request_id=%s",
                exc.category,
                exc.trace_id,
                exc.request_id or _request_correlation_ids(request)[0] or "-",
            )
            raise _character_lookup_http_exception(exc, request=request) from None
        except Exception as exc:
            # Custom/local config providers may still raise a raw DBAPI error;
            # classify it here rather than echoing ``str(exc)`` in JSON.
            logger.error(
                "Failed to get characters: exception_type=%s",
                type(exc).__name__,
            )
            raise _character_lookup_http_exception(exc, request=request) from None

    # ── Settings API Endpoints ──────────────────────────────────────────
    # Allowed settings that can be modified via WebUI
    ALLOWED_SETTINGS = {
        # The Muse remote target is one server-owned document with a dedicated
        # strict validator below; child dotted keys are intentionally absent.
        _MUSE_REMOTE_TARGET_KEY: {"type": "object"},
        "muse_chat.native_link": {"type": "bool"},
        _PC_BRIDGE_REMOTE_ENABLED_KEY: {"type": "bool"},
        "crowdworks.agent_scan_shadow_enabled": {"type": "bool"},
        "crowdworks.agent_scan_policy_id": {
            "type": "uuid",
            "nullable": True,
            "empty_means_null": True,
        },
        # Provider visibility is a global, UI-only preference.  The enclosing
        # application config is DB-backed and the route's existing admin gate
        # protects writes; it is deliberately not consulted by LLM execution
        # or session authorization.
        "llm_provider_visibility": {"type": "object"},
        "llm_provider_visibility.hidden_provider_ids": {"type": "str_list"},
        "external_llm.auto_approve": {"type": "bool"},
        "local_agent_tasks.default_agent_id": {"type": "str"},
        # Persistent command-workspace exemptions. Default is an empty
        # document, which keeps the harness-root confinement.
        "local_agent_tasks.command_workspace_unconfined": {"type": "object"},
        # Trusted harness root for scoped local-agent commands
        # (local_agent_task_adapter.command_workspace_root).  Admin-only like
        # every key here; value must be an existing absolute directory, and
        # null/"" clears it.
        "harness_execution.workspace_root": {"type": "str"},
        # This is the sole PATCH-configurable Trusted Windows allow-list key.
        # Other trusted_windows controls remain outside the WebUI settings API.
        CLI_TRUSTED_WINDOWS_AGENT_IDS_SETTING_KEY: {
            "type": "uuid_list",
            "max": _CLI_TRUSTED_WINDOWS_AGENT_IDS_MAX_ITEMS,
        },
        "integrations.spotify.enabled": {"type": "bool"},
        # 外部送信ポリシーはモデル役割の設定と独立させる。
        "external_model_privacy.mode": {
            "type": "enum",
            "values": ["direct", "protected", "local_only"],
        },
        "external_model_privacy.ip_address_policy": {
            "type": "enum",
            "values": ["legacy", "off", "private", "all"],
        },
        "external_model_privacy.review_policy": {
            "type": "enum",
            "values": ["never", "high_risk", "always"],
        },
        "external_model_privacy.notify": {"type": "bool"},
        "external_model_privacy.semantic_redaction_enabled": {"type": "bool"},
        "external_model_privacy.local_provider": {
            "type": "enum",
            "values": ["ollama", "sglang", "openai_compatible_local"],
        },
        "external_model_privacy.local_model": {"type": "str"},
        "external_model_privacy.redaction_terms": {"type": "str_list"},
        "external_model_privacy.trusted_local_hosts": {"type": "str_list"},
        "external_model_privacy.raw_media_policy": {
            "type": "enum",
            "values": ["block", "confirm"],
        },
        "external_model_privacy.cache_enabled": {"type": "bool"},
        # Cloud Advisor is an independent parent-owned advisory capability;
        # route/policy controls are intentionally separate from role routing
        # topology and never expose credentials or review payloads.
        "browser_agent": {"type": "object"},
        "cloud_advisor.mode": {
            "type": "enum",
            "values": ["disabled", "manual", "automatic"],
        },
        "cloud_advisor.provider": {
            "type": "enum",
            # Web ChatGPT is a first-class Cloud Advisor route, but it is a
            # parent-owned browser façade rather than an API-provider factory
            # target.  Credentials/profile details remain out of settings.
            "values": ["openai", "deepinfra", "chatgpt-web"],
        },
        "cloud_advisor.model": {"type": "str"},
        "cloud_advisor.reasoning_effort": {
            "type": "enum",
            "values": [
                "none",
                "minimal",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            ],
        },
        "chatgpt_web.profile_dir": {"type": "str"},
        "chatgpt_web.response_timeout_seconds": {
            "type": "int",
            "min": 1,
            "max": 3600,
        },
        "chatgpt_web.max_rounds_per_turn": {
            "type": "int",
            "min": 1,
            "max": 100,
        },
        # OpenAI データ共有インセンティブ（無料枠）の推定設定
        "openai.data_sharing_incentive_enabled": {"type": "bool"},
        "openai.usage_tier": {
            "type": "enum",
            "values": ["tier_1_2", "tier_3_plus"],
        },
        "openai.usage_api.refresh_seconds": {
            "type": "int",
            "min": 60,
            "max": 3600,
        },
        "openai.usage_api.unavailable_margin_ratio": {
            "type": "float",
            "min": 0.0,
            "max": 1.0,
        },
        "openai.provider_circuit.probe_interval_minutes": {
            "type": "int",
            "min": 5,
            "max": 1440,
        },
        "openai.billing_scope_id": {"type": "str"},
        "live_voice.openai_realtime.daily_spend_cap_usd": {
            "type": "float",
            "min": 0.0,
            "max": 1000.0,
        },
        "search.knowledge_enabled": {"type": "bool"},
        # User-requested EmbeddingGemma2 master switch (typed bool; persisted
        # through the existing PATCH settings machinery unchanged).
        "search.embedding_enabled": {"type": "bool"},
        "search.embedding_resident": {"type": "bool"},
        # The matching ``enable_tools_source`` marker is written by the store
        # in the same atomic patch; it is deliberately not a settable key.
        "openrouter.enable_tools": {"type": "bool"},
        "reasoning.enabled": {"type": "bool"},
        "reasoning.display_mode": {
            "type": "enum",
            "values": ["silent", "progress", "detailed", "debug"],
        },
        "search.provider": {
            "type": "enum",
            "values": ["openai", "local"],
        },
        "search.openai_model": {"type": "str"},
        "search.jev_rerank_enabled": {"type": "bool"},
        "search.jev_rerank_searxng_public": {"type": "bool"},
        # Agent/Tool toggles
        "agents.filesystem.enabled": {"type": "bool"},
        "agents.project_management.enabled": {"type": "bool"},
        # Optional personal deadline chat draft tool; defaults off and is
        # managed through the existing admin-only settings API.
        _PERSONAL_DEADLINE_CHAT_DRAFT_KEY: {"type": "bool"},
        # External DOTS dialogue remains off unless the fixed, owner-bound
        # dedicated connection is configured and the administrator opts in.
        "dots_dialogue.enabled": {"type": "bool"},
        "mcp_enabled": {"type": "bool"},
        "tts.yomi_linter.enabled": {"type": "bool"},
        "tts.yomi_linter.model_id": {"type": "str"},
        "tts.yomi_linter.device": {
            "type": "enum",
            "values": ["cpu", "cuda", "auto"],
        },
        "tts.yomi_linter.quantization": {
            "type": "enum",
            "values": ["int8", "none"],
        },
        "tts.yomi_linter.confidence_threshold": {
            "type": "float",
            "min": 0.0,
            "max": 1.0,
        },
        "tts.yomi_linter.log_detections": {"type": "bool"},
        "model_routing.media.image_mode": {
            "type": "enum",
            "values": ["auto", "always", "off"],
        },
        "model_routing.media.video_mode": {
            "type": "enum",
            "values": ["auto", "off"],
        },
        "mage_vl.enabled": {"type": "bool"},
        "mage_vl.managed": {"type": "bool"},
        "mage_vl.preload_on_start": {"type": "bool"},
        "mage_vl.model": {"type": "str"},
        "mage_vl.base_url": {"type": "str"},
        "mage_vl.api_key": {"type": "str"},
        "mage_vl.server_command": {"type": "str"},
        "mage_vl.startup_timeout_seconds": {"type": "int", "min": 1, "max": 3600},
        "mage_vl.inference_timeout_seconds": {"type": "int", "min": 1, "max": 7200},
        "mage_vl.max_video_bytes": {
            "type": "int",
            "min": 1,
            "max": 524288000,
        },
        "mage_vl.max_video_duration_seconds": {
            "type": "int",
            "min": 0,
            "max": 86400,
        },
        "mage_vl.video_backend": {
            "type": "enum",
            "values": ["frames"],
        },
        "mage_vl.codec_engine": {
            "type": "enum",
            "values": ["traditional", "neural"],
        },
        "mage_vl.num_frames": {"type": "int", "min": 1, "max": 128},
        "mage_vl.max_pixels": {"type": "int", "min": 0, "max": 4000000},
        "mage_vl.max_new_tokens": {"type": "int", "min": 1, "max": 8192},
    }
    from ...llm.manager import TARGET_CLIENT_PROVIDERS

    class_provider_values = {
        "vision": [""] + sorted(MODEL_ROUTING_PROVIDERS - {"claude-cli", "grok-cli", "deepseek"}),
        # 専用client factoryが生成できるproviderだけを許可する。
        # claude/grok はメディア向け互換APIでは使えるが、target factory未対応。
        "clip_ingest": [""] + sorted(MODEL_ROUTING_PROVIDERS),
        "project_automation": [""] + sorted(TARGET_CLIENT_PROVIDERS),
        "video": ["", "mage_vl"],
    }
    for route_class in ("vision", "clip_ingest", "project_automation", "video"):
        ALLOWED_SETTINGS[f"model_routing.classes.{route_class}.inherit"] = {"type": "bool"}
        ALLOWED_SETTINGS[f"model_routing.classes.{route_class}.provider"] = {
            "type": "enum",
            "values": class_provider_values[route_class],
        }
        ALLOWED_SETTINGS[f"model_routing.classes.{route_class}.model"] = {"type": "str"}
        ALLOWED_SETTINGS[f"model_routing.classes.{route_class}.base_url"] = {"type": "str"}
        ALLOWED_SETTINGS[f"model_routing.classes.{route_class}.api_key"] = {"type": "str"}
    ALLOWED_SETTINGS["model_routing.classes.vision.reasoning_effort"] = {"type": "str"}
    ALLOWED_SETTINGS["model_routing.classes.vision.mode"] = {"type": "str"}
    ALLOWED_SETTINGS["model_routing.classes.clip_ingest.reasoning_effort"] = {"type": "str"}
    ALLOWED_SETTINGS["model_routing.classes.clip_ingest.mode"] = {"type": "str"}
    ALLOWED_SETTINGS["model_routing.classes.project_automation.reasoning_effort"] = {
        "type": "str"
    }
    ALLOWED_SETTINGS["model_routing.classes.project_automation.mode"] = {
        "type": "enum",
        "values": ["", "inherit", "dedicated"],
    }
    ALLOWED_SETTINGS["model_routing.classes.video.reasoning_effort"] = {"type": "str"}
    ALLOWED_SETTINGS["model_routing.classes.video.mode"] = {"type": "str"}
    ALLOWED_SETTINGS["model_routing.classes.audio.engine"] = {
        "type": "enum",
        "values": ["speech_recognition", "llm", "off"],
    }
    ALLOWED_SETTINGS["model_routing.classes.audio.inherit"] = {"type": "bool"}
    ALLOWED_SETTINGS["model_routing.classes.audio.reasoning_effort"] = {"type": "str"}
    ALLOWED_SETTINGS["model_routing.classes.audio.mode"] = {"type": "str"}
    for field in ("provider", "model", "base_url", "api_key"):
        ALLOWED_SETTINGS[f"model_routing.classes.audio.{field}"] = (
            {
                "type": "enum",
                "values": ["openai", "gemini", "openrouter", "kimi", "openai_compatible_local", "sglang", "ollama", "antigravity-cli", ""],
            }
            if field == "provider"
            else {"type": "str"}
        )

    def _normalize_provider_visibility_ids(value: Any) -> list[str]:
        """Normalize the global provider hide list without creating an ACL."""

        if isinstance(value, str):
            values = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            values = value
        else:
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            normalized = str(item or "").strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    def _normalize_optional_uuid(value: Any) -> str | None:
        """Normalize a nullable UUID setting; an empty string means unselected."""

        if value is None or value == "":
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        if not isinstance(value, str):
            raise ValueError("Value must be a UUID string or null")
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError):
            raise ValueError("Value must be a valid UUID or null") from None

    @app.get("/api/settings")
    async def get_settings(
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Get configurable settings"""
        try:
            is_admin = await server._is_admin_user(request)
            raw_mage_vl = server.config.get("mage_vl", {}) or {}
            safe_mage_vl = mask_secret_dict(
                dict(raw_mage_vl) if isinstance(raw_mage_vl, dict) else {},
                is_admin=is_admin,
            )
            try:
                from ...services.mage_vl_service import get_mage_vl_service

                safe_mage_vl["state"] = get_mage_vl_service(server.config).status()
            except Exception as exc:
                logger.warning("Mage-VL status could not be read: %s", exc)
                safe_mage_vl["state"] = {"state": "error", "error": str(exc)}

            raw_model_routing = server.config.get("model_routing", {}) or {}
            safe_model_routing = (
                dict(raw_model_routing) if isinstance(raw_model_routing, dict) else {}
            )
            raw_classes = safe_model_routing.get("classes", {}) or {}
            if isinstance(raw_classes, dict):
                safe_model_routing["classes"] = mask_model_routing_classes(
                    raw_classes,
                    is_admin=is_admin,
                )

            from ...services.command_workspace_confinement import stored_unconfined_setting

            local_agent_tasks = {
                "default_agent_id": server.config.get("local_agent_tasks.default_agent_id") or None,
            }
            try:
                crowdworks_agent_scan_policy_id = _normalize_optional_uuid(
                    server.config.get("crowdworks.agent_scan_policy_id")
                )
            except ValueError:
                # Invalid legacy values disable this optional lane in the
                # runtime; present the same effective unselected state here.
                crowdworks_agent_scan_policy_id = None
            if is_admin:
                local_agent_tasks["command_workspace_unconfined"] = stored_unconfined_setting(server.config)
            settings = {
                "live_voice": {
                    "openai_realtime": {
                        "daily_spend_cap_usd": server.config.get(
                            "live_voice.openai_realtime.daily_spend_cap_usd",
                            1.0,
                        )
                    }
                },
                "local_agent_tasks": local_agent_tasks,
                "external_llm": {
                    "auto_approve": server.config.get(
                        "external_llm.auto_approve", True
                    )
                },
                "personal": {
                    "deadline_chat_draft": {
                        "enabled": server.config.get(
                            _PERSONAL_DEADLINE_CHAT_DRAFT_KEY, False
                        )
                    }
                },
                "crowdworks": {
                    "agent_scan_shadow_enabled": (
                        server.config.get(
                            "crowdworks.agent_scan_shadow_enabled", False
                        )
                        is True
                    ),
                    "agent_scan_policy_id": crowdworks_agent_scan_policy_id,
                },
                "llm_provider_visibility": {
                    "hidden_provider_ids": _normalize_provider_visibility_ids(
                        server.config.get(
                            "llm_provider_visibility.hidden_provider_ids", []
                        )
                    )
                },
                "external_model_privacy": {
                    "mode": server.config.get(
                        "external_model_privacy.mode", "direct"
                    ),
                    "ip_address_policy": server.config.get(
                        "external_model_privacy.ip_address_policy", "legacy"
                    ),
                    "review_policy": server.config.get(
                        "external_model_privacy.review_policy", "high_risk"
                    ),
                    "notify": bool(
                        server.config.get("external_model_privacy.notify", True)
                    ),
                    "semantic_redaction_enabled": bool(
                        server.config.get(
                            "external_model_privacy.semantic_redaction_enabled", True
                        )
                    ),
                    "local_provider": server.config.get(
                        "external_model_privacy.local_provider",
                        "openai_compatible_local",
                    ),
                    "local_model": server.config.get(
                        "external_model_privacy.local_model", ""
                    ),
                    "redaction_terms": server.config.get(
                        "external_model_privacy.redaction_terms", []
                    ),
                    "trusted_local_hosts": server.config.get(
                        "external_model_privacy.trusted_local_hosts", []
                    ),
                    "raw_media_policy": server.config.get(
                        "external_model_privacy.raw_media_policy", "block"
                    ),
                    "cache_enabled": bool(
                        server.config.get("external_model_privacy.cache_enabled", True)
                    ),
                },
                "browser_agent": {
                    **browser_settings_payload(server.config),
                    "pc_bridge_required": True,
                    "jev_configured": bool(os.environ.get("JEV_API_KEY", "").strip()),
                },
                "pc_bridge": {
                    "remote_enabled": server.config.get(
                        _PC_BRIDGE_REMOTE_ENABLED_KEY, False
                    ) is True
                },
                "cloud_advisor": {
                    "mode": server.config.get(
                        "cloud_advisor.mode", "disabled"
                    ),
                    "provider": server.config.get(
                        "cloud_advisor.provider", "openai"
                    ),
                    "model": str(
                        server.config.get("cloud_advisor.model", "") or ""
                    ).strip(),
                    "reasoning_effort": server.config.get(
                        "cloud_advisor.reasoning_effort", "high"
                    ),
                },
                "chatgpt_web": {
                    "profile_dir": server.config.get(
                        "chatgpt_web.profile_dir", ""
                    ),
                    "response_timeout_seconds": server.config.get(
                        "chatgpt_web.response_timeout_seconds", 900
                    ),
                    "max_rounds_per_turn": server.config.get(
                        "chatgpt_web.max_rounds_per_turn", 20
                    ),
                },
                "model_routing": safe_model_routing,
                "mage_vl": safe_mage_vl,
                "speech_recognition": server.config.get("speech_recognition", {}),
                "knowledge": {
                    "enabled": server.config.get("search.knowledge_enabled", False)
                },
                "reasoning": {
                    "enabled": server.config.get("reasoning.enabled", False),
                    "display_mode": server.config.get(
                        "reasoning.display_mode", "progress"
                    ),
                },
                "search": {
                    "provider": server.config.get("search.provider", "openai"),
                    "jev_rerank_enabled": server.config.get("search.jev_rerank_enabled", False),
                    "jev_rerank_searxng_public": server.config.get("search.jev_rerank_searxng_public", False),
                    "openai_model": str(
                        server.config.get("search.openai_model") or ""
                    ).strip(),
                    "knowledge_enabled": server.config.get(
                        "search.knowledge_enabled", False
                    ),
                    # Master ON/OFF for user-requested EmbeddingGemma2 usage.
                    # Effective boolean: absent key keeps the historical default.
                    "embedding_resident": server.config.get("search.embedding_resident", False) is True,
                    "embedding_enabled": embedding_enabled(
                        {"search": server.config.get("search", {})}
                    ),
                },
                "agents": {
                    "filesystem": {
                        "enabled": server.config.get(
                            "agents.filesystem.enabled", True
                        )
                    },
                    "project_management": {
                        "enabled": server.config.get(
                            "agents.project_management.enabled", True
                        )
                    },
                    "mcp": {
                        "enabled": (
                            not Features.is_enterprise()
                            and server.config.get("mcp_enabled", True)
                        )
                    },
                    "spotify": {
                        "enabled": server.config.get(
                            "integrations.spotify.enabled",
                            server.config.get("spotify.enabled", False),
                        )
                    },
                },
                "integrations": {
                    "spotify": {
                        "enabled": server.config.get(
                            "integrations.spotify.enabled", False
                        )
                    }
                },
                "spotify": {
                    "enabled": server.config.get("integrations.spotify.enabled", False)
                },
                "tts": {
                    "yomi_linter": {
                        "enabled": server.config.get("tts.yomi_linter.enabled", False),
                        "model_id": server.config.get(
                            "tts.yomi_linter.model_id",
                            "ayousanz/yomi-linter-modernbert-ja-130m",
                        ),
                        "device": server.config.get("tts.yomi_linter.device", "cpu"),
                        "quantization": server.config.get(
                            "tts.yomi_linter.quantization", "int8"
                        ),
                        "confidence_threshold": server.config.get(
                            "tts.yomi_linter.confidence_threshold", 0.5
                        ),
                        "log_detections": server.config.get(
                            "tts.yomi_linter.log_detections", True
                        ),
                    }
                },
            }
            # Remote target values are never returned directly.  Even the
            # presence summary is administrator-only and hidden by profiles
            # where remote-server access is unavailable.
            if is_admin and _muse_remote_target_profile_enabled():
                settings["remote_targets"] = {
                    "muse": _muse_remote_target_summary(
                        server.config.get(_MUSE_REMOTE_TARGET_KEY, {})
                    )
                }
            return JSONResponse({"settings": settings, "schema": ALLOWED_SETTINGS})
        except Exception as e:
            logger.error(f"Failed to get settings: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get(
        "/api/model-roles",
        response_model=ModelRolesViewSchema,
        responses=_MODEL_ROLES_AUTH_RESPONSES,
    )
    async def get_model_roles(
        _: None = Depends(require_auth),
    ):
        return JSONResponse(model_roles_view(server.config))

    @app.put(
        "/api/model-roles",
        response_model=ModelRolesViewSchema,
        openapi_extra=_MODEL_ROLES_UPDATE_OPENAPI,
        responses={
            **_MODEL_ROLES_AUTH_RESPONSES,
            400: {
                "model": ModelRolesErrorResponse,
                "description": "Invalid JSON payload or model role settings",
            },
            403: {
                "model": ModelRolesErrorResponse,
                "description": "Administrator privileges required",
            },
            409: {
                "model": ModelRolesErrorResponse,
                "description": "The expected model_roles version is stale",
                "content": {
                    "application/json": {
                        "example": {"detail": "model_roles_version_conflict"}
                    }
                },
            },
            500: {
                "model": ModelRolesErrorResponse,
                "description": "The model_roles setting could not be persisted",
            },
        },
    )
    async def put_model_roles(
        request: Request,
        _: None = Depends(require_auth),
    ):
        if not await server._is_admin_user(request):
            raise HTTPException(status_code=403, detail="Administrator privileges required")
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="JSON payload is required") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="model_roles payload must be an object")
        if set(payload) != {"expected_version", "roles"}:
            raise HTTPException(
                status_code=400,
                detail="model_roles payload requires expected_version and roles",
            )
        expected_version = payload.get("expected_version")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise HTTPException(status_code=400, detail="expected_version must be a non-negative integer")
        roles = payload.get("roles")
        if not isinstance(roles, dict):
            raise HTTPException(status_code=400, detail="roles must be an object")
        try:
            result = update_model_roles(
                server.config,
                roles,
                expected_version,
            )
        except ModelRolesVersionConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="model_roles_version_conflict",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ModelRolesPersistenceError as exc:
            logger.error("Failed to persist model_roles configuration")
            raise HTTPException(
                status_code=500,
                detail="Failed to persist model_roles configuration",
            ) from exc
        return JSONResponse(result)

    async def canonical_admin(request: Request) -> str:
        scope = dict(request.scope)
        scope["headers"] = [(k, v) for k, v in request.scope.get("headers", [])
                            if k.lower() != b"x-internal-auth" and not k.lower().startswith(b"x-forwarded-")]
        user = await server._get_user_info_from_request(Request(scope))
        if (not isinstance(user, dict) or str(user.get("role", "")).lower() != "admin"
                or user.get("is_agent") or user.get("_authority_source") not in {"web_session", "bearer"}
                or any(user.get(k) not in {None, "", "human", "user"}
                       for k in ("actor_type", "principal_kind"))):
            raise HTTPException(status_code=403, detail="Administrator privileges required")
        try:
            owner = str(uuid.UUID(str(user.get("id"))))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=403, detail="Authenticated human UUID is required") from None
        if not _muse_remote_target_profile_enabled():
            raise HTTPException(status_code=403, detail="This setting is disabled in the current profile")
        return owner

    @app.get("/api/settings/remote-targets/muse/canonical-agent")
    async def get_muse_canonical_agent(request: Request, _: None = Depends(require_auth)):
        await canonical_admin(request)
        return _muse_canonical_public(_read_stored_muse_target())

    @app.put("/api/settings/remote-targets/muse/canonical-agent")
    async def put_muse_canonical_agent(payload: MuseCanonicalUpdate, request: Request,
                                       _: None = Depends(require_auth)):
        owner = await canonical_admin(request)
        for _ in range(3):
            target = _read_stored_muse_target()
            if (not isinstance(target, dict) or not isinstance(target.get("allowed_owner_ids"), list)
                    or owner not in target["allowed_owner_ids"]):
                raise HTTPException(status_code=403, detail="Muse owner is not allowed")
            current = _muse_canonical_public(target)
            expected = payload.expected
            if ((expected is None and current["configured"]) or
                    (expected is not None and (current["agent_id"], current["revision_id"]) !=
                     (str(expected.agent_id), str(expected.revision_id)))):
                raise HTTPException(status_code=409, detail="Muse canonical binding changed")
            agent_id, revision_id = str(payload.pair.agent_id), str(payload.pair.revision_id)
            if not payload.pair.agent_id.int or not payload.pair.revision_id.int:
                raise HTTPException(status_code=422, detail="Invalid canonical binding")
            agents = target.get("allowed_agent_ids")
            if not isinstance(agents, list) or agent_id not in agents:
                raise HTTPException(status_code=403, detail="Muse agent is not allowed")
            from sqlalchemy import select
            from ...memory.models.agent_identity import Agent, AgentRevision
            async with await server._db_manager.get_session() as session:
                row = (await session.execute(select(Agent.id, Agent.created_by, Agent.state,
                        AgentRevision.id).join(AgentRevision, AgentRevision.agent_id == Agent.id)
                        .where(Agent.id == payload.pair.agent_id,
                               AgentRevision.id == payload.pair.revision_id))).one_or_none()
            if row is None or row.state != "active" or str(row.created_by) != owner:
                raise HTTPException(status_code=404, detail="Eligible Agent revision not found")
            updated = copy.deepcopy(target)
            updated["chat_agent_id"] = agent_id
            updated["chat_agent_revision_id"] = revision_id
            for alias in ("agent_revision_id", "revision_id"):
                if alias in updated:
                    updated[alias] = revision_id
            if _muse_canonical_binding(updated) is None:
                raise HTTPException(status_code=409, detail="Conflicting legacy Muse binding")
            if update_app_config_keys_sync({_MUSE_REMOTE_TARGET_KEY: updated},
                    expected_values={_MUSE_REMOTE_TARGET_KEY: target}):
                server.config.set(_MUSE_REMOTE_TARGET_KEY, updated)
                return _muse_canonical_public(updated)
        raise HTTPException(status_code=409, detail="Muse target changed or could not be saved")

    @app.post("/api/settings/remote-targets/muse/allowed-agents")
    async def register_muse_allowed_agent(
        payload: MuseAllowedAgentPayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        # Resolve the principal independently of any client-supplied identity.
        scope = dict(request.scope)
        scope["headers"] = [
            (key, value) for key, value in request.scope.get("headers", [])
            if key.lower() != b"x-internal-auth" and not key.lower().startswith(b"x-forwarded-")
        ]
        user = await server._get_user_info_from_request(Request(scope))
        if (not isinstance(user, dict) or str(user.get("role", "")).lower() != "admin"
                or user.get("is_agent") or user.get("_authority_source") not in {"web_session", "bearer"}
                or any(user.get(key) not in {None, "", "human", "user"}
                       for key in ("actor_type", "principal_kind"))):
            raise HTTPException(status_code=403, detail="Administrator privileges required")
        try:
            owner_id = str(uuid.UUID(str(user.get("id"))))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=403, detail="Authenticated human UUID is required") from None
        if not _muse_remote_target_profile_enabled():
            raise HTTPException(status_code=403, detail="This setting is disabled in the current profile")

        from ...services.agent_identity_service import AgentIdentityService
        from ...services.agent_revision_v2 import resolve_agent_role_route_v2

        agent_id = str(payload.agent_id)
        identity = AgentIdentityService(server._db_manager, config=server.config)
        agent = await identity.get_agent(agent_id)
        if not agent or agent.get("state") != "active" or str(agent.get("created_by")) != owner_id:
            raise HTTPException(status_code=404, detail="Eligible Agent not found")
        revisions = await identity.list_revisions(agent_id, limit=1)
        if not revisions or "remote_agent_delegate" not in revisions[0].get("capability_ceiling", []):
            raise HTTPException(status_code=404, detail="Eligible Agent not found")
        try:
            route, _source = resolve_agent_role_route_v2(server.config, revisions[0], "operator")
        except (ValueError, RuntimeError):
            route = None
        if not isinstance(route, dict) or (route.get("kind"), route.get("provider"), route.get("model")) != ("llm", "muse-remote", "muse-chat"):
            raise HTTPException(status_code=404, detail="Eligible Agent not found")

        # Compare the complete target under the existing store's row lock so
        # owner authorization and all opaque options survive concurrent edits.
        for _attempt in range(3):
            config = load_stored_app_config_sync()
            target = config.get("remote_targets", {}).get("muse")
            if not isinstance(target, dict):
                raise HTTPException(status_code=403, detail="Muse owner is not allowed")
            owners = target.get("allowed_owner_ids")
            if not isinstance(owners, list) or owner_id not in owners:
                raise HTTPException(status_code=403, detail="Muse owner is not allowed")
            agents = target.get("allowed_agent_ids")
            if not isinstance(agents, list):
                raise HTTPException(status_code=409, detail="Invalid Muse agent allowlist")
            updated = copy.deepcopy(target)
            if agent_id not in agents:
                updated["allowed_agent_ids"].append(agent_id)
            if update_app_config_keys_sync(
                {_MUSE_REMOTE_TARGET_KEY: updated},
                expected_values={_MUSE_REMOTE_TARGET_KEY: target},
            ):
                server.config.set(_MUSE_REMOTE_TARGET_KEY, updated)
                return {"agent_id": agent_id, "allowed_agent_count": len(updated["allowed_agent_ids"])}
        raise HTTPException(status_code=409, detail="Muse target changed or could not be saved")

    @app.patch("/api/settings")
    async def update_setting(
        payload: SettingsPayload,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Update a configuration setting"""
        key = payload.key
        value = payload.value
        persist = payload.persist

        if key == _MUSE_REMOTE_TARGET_KEY:
            # The remote target is a privileged, profile-gated server setting
            # even when cookie authentication is disabled for local operation.
            if not await server._is_admin_user(request):
                raise HTTPException(
                    status_code=403,
                    detail="Administrator privileges required",
                )
            if not _muse_remote_target_profile_enabled():
                raise HTTPException(
                    status_code=403,
                    detail="This setting is disabled in the current profile",
                )
            try:
                raw_request_payload = await request.json()
            except Exception:
                raw_request_payload = None
            if (
                not isinstance(raw_request_payload, dict)
                or raw_request_payload.get("persist") is not True
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Muse remote target settings must be persisted",
                )
            pin_keys = {"chat_agent_id", "chat_agent_revision_id", "agent_revision_id", "revision_id"}
            if isinstance(value, dict) and pin_keys & value.keys():
                raise HTTPException(status_code=409, detail="Use canonical binding settings")
            try:
                value = _validate_muse_remote_target(value)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid Muse remote target settings",
                ) from None

            try:
                for _ in range(3):
                    target = _read_stored_muse_target()
                    updated = copy.deepcopy(value)
                    if isinstance(target, dict):
                        for pin in pin_keys:
                            if pin in target:
                                updated[pin] = copy.deepcopy(target[pin])
                    if update_app_config_keys_sync(
                        {key: updated}, expected_values={key: target}
                    ):
                        server.config.set(key, updated)
                        value = updated
                        break
                else:
                    raise HTTPException(status_code=409, detail="Muse target changed or could not be saved")

                logger.info(
                    "Setting updated: %s = %s (persist=%s)",
                    key,
                    format_setting_log_value(key, value),
                    True,
                )
                return JSONResponse(
                    public_setting_patch_payload(key, value, persisted=True)
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.error(
                    "Failed to update setting: key=%s exception_type=%s",
                    key,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Failed to update setting",
                ) from None

        if key in {_EMBEDDING_MASTER_KEY, "search.embedding_resident"}:
            # The embedding master switch is a privileged server setting even
            # when cookie authentication is disabled for local operation, the
            # same boundary as the Muse remote target above.  Refuse here so a
            # non-admin request is rejected before value validation,
            # persistence and any runtime notification.
            if not await server._is_admin_user(request):
                raise HTTPException(
                    status_code=403,
                    detail="Administrator privileges required",
                )

        if key == _PERSONAL_DEADLINE_CHAT_DRAFT_KEY:
            # This is a privileged feature gate even when cookie auth is
            # disabled for local operation.
            if not await server._is_admin_user(request):
                raise HTTPException(
                    status_code=403,
                    detail="Administrator privileges required",
                )

        if getattr(server, "auth_enabled", True) and not await server._is_admin_user(
            request
        ):
            raise HTTPException(
                status_code=403, detail="Administrator privileges required"
            )

        if is_admin_only_setting_key(key) and not await server._is_admin_user(request):
            raise HTTPException(
                status_code=403, detail="Administrator privileges required"
            )

        if key == "local_agent_tasks.command_workspace_unconfined":
            # Auth-disabled mode still cannot publish this exemption.
            if not await server._is_admin_user(request):
                raise HTTPException(status_code=403, detail="Administrator privileges required")

        if key == "local_agent_tasks.default_agent_id":
            # This global selection remains admin-only in auth-disabled mode.
            if not await server._is_admin_user(request):
                raise HTTPException(status_code=403, detail="Administrator privileges required")
            from ...services.local_agent_task_adapter import validate_default_agent_id
            from ...services.local_agent_tasks import LocalAgentTaskError
            from ...services.conversation_agent_service import ConversationAgentError
            from ...services.agent_model_routing import AgentModelRouteError

            user_info = await server._get_user_info_from_request(request)
            try:
                value = await validate_default_agent_id(
                    server.config, value,
                    actor_user_id=(user_info or {}).get("id"), actor_role="admin",
                )
            except (LocalAgentTaskError, ConversationAgentError, AgentModelRouteError) as exc:
                raise HTTPException(
                    status_code=getattr(
                        exc, "http_status", getattr(exc, "status_code", 409)
                    ),
                    detail={"code": exc.code, "message": "The default local Agent selection is invalid"},
                ) from None

        # Validate the key is allowed
        if key not in ALLOWED_SETTINGS:
            raise HTTPException(
                status_code=400,
                detail=f"Setting '{key}' is not configurable via WebUI",
            )

        # Validate value type and constraints
        setting_schema = ALLOWED_SETTINGS[key]
        try:
            if setting_schema["type"] == "bool":
                if key in {
                    _PERSONAL_DEADLINE_CHAT_DRAFT_KEY,
                    "search.embedding_resident",
                    _PC_BRIDGE_REMOTE_ENABLED_KEY,
                    "crowdworks.agent_scan_shadow_enabled",
                }:
                    if not isinstance(value, bool):
                        raise ValueError(f"Setting '{key}' must be a boolean")
                elif not isinstance(value, bool):
                    value = str(value).lower() in ("true", "1", "yes")
            elif setting_schema["type"] == "float":
                value = float(value)
                if "min" in setting_schema and value < setting_schema["min"]:
                    raise ValueError(f"Value must be >= {setting_schema['min']}")
                if "max" in setting_schema and value > setting_schema["max"]:
                    raise ValueError(f"Value must be <= {setting_schema['max']}")
            elif setting_schema["type"] == "enum":
                if value not in setting_schema["values"]:
                    raise ValueError(
                        f"Value must be one of: {setting_schema['values']}"
                    )
            elif setting_schema["type"] == "str":
                if key == "local_agent_tasks.default_agent_id":
                    # Already validated; preserve null as an explicit clear.
                    pass
                elif key == "harness_execution.workspace_root":
                    value = _validate_harness_workspace_root(value)
                elif key == "cloud_advisor.model":
                    # Empty means use canonical_model_for_provider() at
                    # consultation time; this is an intentional setting.
                    value = "" if value is None else str(value).strip()
                else:
                    if key == "search.openai_model" and (
                        not isinstance(value, str) or not value.strip()
                    ):
                        raise ValueError("OpenAI検索モデルを指定してください")
                    value = str(value).strip()
                    if key == "chatgpt_web.profile_dir" and not value:
                        raise ValueError("ChatGPT会話プロファイルの保存先を指定してください")
            elif setting_schema["type"] == "uuid":
                value = _normalize_optional_uuid(value)
            elif setting_schema["type"] == "int":
                if isinstance(value, bool):
                    raise ValueError(f"Setting '{key}' must be an integer")
                if isinstance(value, float) and not value.is_integer():
                    raise ValueError(f"Setting '{key}' must be an integer")
                try:
                    value = int(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"Setting '{key}' must be an integer") from exc
                if "min" in setting_schema and value < setting_schema["min"]:
                    raise ValueError(
                        f"Setting '{key}' must be >= {setting_schema['min']}"
                    )
                if "max" in setting_schema and value > setting_schema["max"]:
                    raise ValueError(
                        f"Setting '{key}' must be <= {setting_schema['max']}"
                    )
            elif setting_schema["type"] == "str_list":
                if isinstance(value, str):
                    value = [
                        item.strip()
                        for item in value.split(",")
                        if item.strip()
                    ]
                elif isinstance(value, list):
                    value = [
                        str(item or "").strip()
                        for item in value
                        if str(item or "").strip()
                    ]
                else:
                    raise ValueError("Value must be a list of strings")
                if key == "llm_provider_visibility.hidden_provider_ids":
                    value = _normalize_provider_visibility_ids(value)
            elif setting_schema["type"] == "uuid_list":
                if not isinstance(value, list) or not value:
                    raise ValueError("Value must be a non-empty list of UUID strings")
                if len(value) > setting_schema["max"]:
                    raise ValueError(
                        f"Value must contain no more than {setting_schema['max']} UUIDs"
                    )
                normalized_ids: list[str] = []
                seen_ids: set[str] = set()
                for item in value:
                    if not isinstance(item, str):
                        raise ValueError("Value must contain only UUID strings")
                    try:
                        normalized_id = str(uuid.UUID(item))
                    except (ValueError, AttributeError):
                        raise ValueError("Value must contain only UUID strings") from None
                    if normalized_id in seen_ids:
                        raise ValueError("Value must not contain duplicate UUIDs")
                    seen_ids.add(normalized_id)
                    normalized_ids.append(normalized_id)
                value = normalized_ids
            elif setting_schema["type"] == "object":
                if key == "browser_agent":
                    value = validate_browser_settings_update(value)
                if key == "local_agent_tasks.command_workspace_unconfined":
                    from ...services.command_workspace_confinement import (
                        ensure_unconfined_subjects,
                        normalize_unconfined_setting,
                    )

                    value = normalize_unconfined_setting(value)
                    await ensure_unconfined_subjects(value)
                if not isinstance(value, dict):
                    raise ValueError("Value must be an object")
                if key == "llm_provider_visibility":
                    value = {
                        "hidden_provider_ids": _normalize_provider_visibility_ids(
                            value.get("hidden_provider_ids", [])
                        )
                    }
            if key.startswith("model_routing.classes.project_automation."):
                from ...services.project_automation_model import (
                    ProjectAutomationRouteError,
                    resolve_project_automation_route,
                )

                current_route = server.config.get(
                    "model_routing.classes.project_automation",
                    {},
                )
                candidate_route = (
                    dict(current_route)
                    if isinstance(current_route, dict)
                    else {}
                )
                route_field = key.rsplit(".", 1)[-1]
                candidate_route[route_field] = value
                candidate_config = {
                    "llm_provider": server.config.get("llm_provider", ""),
                    "llm_model": server.config.get("llm_model", ""),
                    "model_routing": {
                        "classes": {
                            "project_automation": candidate_route,
                        }
                    },
                }
                try:
                    resolve_project_automation_route(candidate_config)
                except ProjectAutomationRouteError as exc:
                    raise ValueError(str(exc)) from exc
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Enterprise SGLang is a paired Compose service, not a user-selectable
        # arbitrary OpenAI-compatible endpoint.  Reject route values at the
        # settings boundary as well as in the client factory, so clip ingest
        # and role routing cannot persist a model that the server does not serve.
        if Features.is_enterprise():
            route_prefix = None
            route_field = None
            parts = key.split(".")
            if (
                len(parts) == 4
                and parts[:2] == ["model_routing", "classes"]
                and parts[-1] in {"provider", "model"}
            ):
                route_prefix = ".".join(parts[:3])
                route_field = parts[-1]
            elif (
                len(parts) == 4
                and parts[:2] == ["model_routing", "overrides"]
                and parts[-1] in {"provider", "model"}
            ):
                route_prefix = ".".join(parts[:3])
                route_field = parts[-1]
            if route_prefix and route_field:
                provider = (
                    str(value or "").strip()
                    if route_field == "provider"
                    else str(server.config.get(f"{route_prefix}.provider", "") or "").strip()
                )
                model = (
                    str(value or "").strip()
                    if route_field == "model"
                    else str(server.config.get(f"{route_prefix}.model", "") or "").strip()
                )
                if provider.lower() == "sglang" and model:
                    try:
                        enforce_enterprise_sglang_model(
                            server.config, provider, model
                        )
                    except (RuntimeError, ValueError) as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc

        if Features.is_enterprise() and (
            key in {
                "mcp_enabled",
                "dots_dialogue.enabled",
            }
            or key.startswith("model_routing.media.")
        ) and value is not False:
            raise HTTPException(
                status_code=403,
                detail="This setting is disabled in the Enterprise profile",
            )

        # The embedding master flag must land in the DB: the runtime observer
        # reads the persisted row, so an in-memory-only update would silently
        # desync the gate from what the operator approved.  Refuse before any
        # config mutation.
        if key in {_EMBEDDING_MASTER_KEY, "search.embedding_resident"} and not persist:
            raise HTTPException(
                status_code=400,
                detail=f"{key} must be persisted",
            )

        # Apply the setting
        try:
            if persist:
                # Save to both memory and DB
                success = server.config.save_to_file(key, value)
                if not success:
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to persist setting to DB",
                    )
                if key == OPENROUTER_ENABLE_TOOLS_KEY:
                    # The store wrote the provenance with the value; mirror it.
                    server.config.set(
                        OPENROUTER_ENABLE_TOOLS_SOURCE_PATH,
                        OPENROUTER_ENABLE_TOOLS_USER_PROVENANCE,
                    )
            else:
                # Only update in memory
                server.config.set(key, value)

            # Persistence succeeded (the call above raises otherwise), so the
            # DB row now carries the master flag; tell an already-live embedding
            # wrapper without creating one.  The shared observation cache is
            # dropped first so no runtime notification can leave a stale ON.
            if key == _EMBEDDING_MASTER_KEY:
                invalidate_runtime_embedding_cache()
                _sync_embedding_master_runtime(value)
                if value and server.config.get("search.embedding_resident", False) is True:
                    server.apply_embedding_resident(True)

            if key == "search.embedding_resident":
                server.apply_embedding_resident(value)

            if key == "browser_agent":
                from ...services.pc_bridge_service import get_pc_bridge_hub
                await get_pc_bridge_hub().sync_dedicated_gate()

            if key == _PC_BRIDGE_REMOTE_ENABLED_KEY and value is False:
                from ...services.pc_bridge_service import get_pc_bridge_hub

                hub = get_pc_bridge_hub()
                with hub.lock:
                    connections = list(hub.connections.values())
                for connection in connections:
                    try:
                        await asyncio.wait_for(
                            hub._on_loop(
                                connection,
                                connection.socket.close(
                                    code=4003,
                                    reason="pc_bridge_remote_disabled",
                                ),
                            ),
                            timeout=5,
                        )
                    except Exception:
                        logger.warning(
                            "PC Bridge connection close failed after remote disable"
                        )
                    finally:
                        hub.detach(connection)

            logger.info(
                "Setting updated: %s = %s (persist=%s)",
                key,
                format_setting_log_value(key, value),
                persist,
            )
            return JSONResponse(
                public_setting_patch_payload(key, value, persisted=persist)
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Failed to update setting: key=%s exception_type=%s",
                key,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to update setting",
            ) from None

    @app.post("/api/character/{character_name}")
    async def switch_character(
        character_name: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Switch to a different character"""
        try:
            # Validate character exists before mutating user/session state.
            char_config = await asyncio.to_thread(
                server.config.get_character_config,
                character_name,
            )
            db_character = char_config.get("_db_character", {})
            canonical_character_name = (
                str(db_character.get("slug") or character_name).strip()
            )

            auth_enabled = bool(getattr(server, "auth_enabled", True))
            is_admin = await server._is_admin_user(request)
            user_info = await server._get_user_info_from_request(request)
            user_id = str(
                (user_info or {}).get("id")
                or ("default_user" if not auth_enabled else "")
            ).strip()

            # Resolve the actual target session before changing global or
            # preferred-character state.  The durable Root row is the
            # authority; a client-supplied session marker is only a lookup
            # hint.  This prevents a Root Character mutation from partially
            # applying the global setting before the 409 is returned.
            if user_id:
                from ...memory.conversation_repository import ConversationRepository
                from ...memory.database import get_database_manager
                from ...memory.models import ConversationSession
                from ...services.agent_root_lifecycle import guard_private_root_update

                target_lookup = ConversationRepository()
                requested_session_id = request.query_params.get("session_id")
                try:
                    if requested_session_id:
                        target_for_guard = await target_lookup.get_session_by_id(
                            requested_session_id,
                            with_messages=False,
                        )
                    else:
                        active_for_guard = await target_lookup.get_user_sessions(
                            user_id,
                            limit=20,
                            include_inactive=False,
                        )
                        target_for_guard = next(
                            (
                                candidate
                                for candidate in active_for_guard
                                if not bool(getattr(candidate, "is_group_chat", False))
                            ),
                            None,
                        )
                except (TypeError, ValueError):
                    target_for_guard = None
                if target_for_guard is not None and str(target_for_guard.user_id) == user_id:
                    guard_session = await get_database_manager().get_session()
                    try:
                        locked_target = await guard_session.get(
                            ConversationSession,
                            target_for_guard.id,
                        )
                        if locked_target is not None:
                            await guard_private_root_update(
                                guard_session,
                                locked_target,
                                fields={"character_name"},
                                actor_user_id=user_id,
                            )
                    finally:
                        await guard_session.close()

            if auth_enabled is False or is_admin:
                # Keep manager callbacks on the FastAPI event-loop thread.
                # The WebSocket notifier schedules a coroutine via the running
                # loop; moving this call to ``to_thread`` would silently skip
                # that notification and allow concurrent switches to race.
                character_manager = CharacterSwitchManager()
                success = bool(
                    character_manager.switch_character(
                        character_name,
                        canonical_character_name,
                    )
                )
                if not success:
                    raise HTTPException(
                        status_code=500, detail="Failed to switch character"
                    )
            else:
                success = True

            if success:
                if auth_enabled is False or is_admin:
                    def _persist_default_character_sync() -> None:
                        if hasattr(server.config, "save_to_file"):
                            if not server.config.save_to_file(
                                "default_character", character_name
                            ):
                                raise RuntimeError("Failed to persist default_character")
                        else:
                            server.config.set("default_character", character_name)

                    await asyncio.to_thread(_persist_default_character_sync)

                    # Update server's character name
                    server.character_name = character_name

                if user_id:
                    try:
                        await update_user_preferred_character(
                            user_id,
                            canonical_character_name,
                        )
                    except Exception as exc:
                        if auth_enabled and not is_admin:
                            raise HTTPException(
                                status_code=503,
                                detail="Failed to persist preferred character",
                            ) from exc
                        logger.warning(
                            "Failed to persist preferred_character for user %s: %s",
                            user_id,
                            exc,
                        )

                # The selected header character also becomes the character for
                # the currently active normal chat session. Scenario/group
                # writing/group sessions remain isolated from the global character switch.
                session_synchronized = False
                session_sync_required = False
                try:
                    if user_info is None and auth_enabled:
                        raise PermissionError(
                            "認証済みユーザーを解決できないため、セッションを同期できません"
                        )
                    if not user_id:
                        raise PermissionError("セッション同期対象のユーザーがありません")

                    from ...memory.conversation_repository import ConversationRepository

                    repo = ConversationRepository()
                    requested_session_id = request.query_params.get("session_id")
                    target_session = None

                    def is_isolated_session(session) -> bool:
                        return bool(getattr(session, "is_group_chat", False))

                    if requested_session_id:
                        target_session = await repo.get_session_by_id(
                            requested_session_id,
                            with_messages=False,
                        )
                        if target_session is not None and (
                            str(target_session.user_id) != user_id
                            or not target_session.is_active
                            or is_isolated_session(target_session)
                        ):
                            target_session = None
                        # An explicit group/scenario/invalid session must not
                        # fall through and mutate an unrelated active session.
                        session_sync_required = target_session is not None
                    else:
                        active_sessions = await repo.get_user_sessions(
                            user_id,
                            limit=20,
                            include_inactive=False,
                        )
                        target_session = next(
                            (
                                session
                                for session in active_sessions
                                if not is_isolated_session(session)
                            ),
                            None,
                        )
                        session_sync_required = target_session is not None
                    if target_session is not None:
                        updated = await repo.update_session(
                            str(target_session.id),
                            character_name=canonical_character_name,
                            touch_activity=False,
                            actor_user_id=user_id,
                        )
                        if not updated:
                            raise RuntimeError("対象セッションを更新できませんでした")
                        session_synchronized = True
                except AgentRootLifecycleError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Failed to synchronize active conversation character: %s",
                        exc,
                    )
                    if session_sync_required or request.query_params.get("session_id"):
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "キャラクターは切り替わりましたが、現在のセッション同期に失敗しました。"
                                "ページを再読み込みして再試行してください。"
                            ),
                        ) from exc

                return JSONResponse(
                    {
                        "success": True,
                        "character": character_name,
                        "character_slug": canonical_character_name,
                        "session_synchronized": session_synchronized,
                        "persisted": True,
                        "message": f"Switched to {character_name}",
                    }
                )
            else:
                raise HTTPException(
                    status_code=500, detail="Failed to switch character"
                )

        except HTTPException:
            raise
        except CharacterNotFoundError as exc:
            raise _character_lookup_http_exception(
                exc,
                request=request,
                not_found_status=404,
            ) from None
        except CharacterLookupError as exc:
            logger.error(
                "Character lookup failed during switch: category=%s trace_id=%s request_id=%s",
                exc.category,
                exc.trace_id,
                exc.request_id or _request_correlation_ids(request)[0] or "-",
            )
            raise _character_lookup_http_exception(exc, request=request) from None
        except AgentRootLifecycleError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except Exception as exc:
            # Keep unrelated switch failures generic; in particular, do not
            # echo a raw SQLAlchemy/DBAPI message that may contain a DSN.
            logger.error(
                "Failed to switch character: exception_type=%s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to switch character",
            ) from None
