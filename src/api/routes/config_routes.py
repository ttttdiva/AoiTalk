"""アプリ設定・音声状態・キャラクター系ルート (server.py から移設)"""

import logging
import re
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

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
from ...services.agent_team_service import (
    AGENT_TEAM_PROVIDERS,
    MODEL_ROUTING_PROVIDERS,
    AGENT_TEAM_SCHEMA_VERSION,
    AGENT_TEAM_CAPABILITY_CATALOG,
    AGENT_TEAM_CONTEXT_TAGS,
    normalize_agent_team_v3,
    agent_team_v3_teams,
    agent_team_v3_subagents,
    resolve_agent_team_v3_route,
)
from ...services.execution_profile_service import (
    validate_execution_route,
    validate_team_execution_profile,
)
from ...llm.sglang_url import enforce_enterprise_sglang_model
from ...agent_harness.config import public_agent_harness_settings
from ...security.settings_public import (
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
        try:
            characters = server.config.get_available_characters()
            option_loader = getattr(
                server.config, "get_available_character_options", None
            )
            character_options = (
                option_loader()
                if callable(option_loader)
                else [{"slug": name, "name": name} for name in characters]
            )
            current_character = await resolve_request_character_name(server, request)
            if any(
                option.get("slug") == "project_manager"
                for option in character_options
                if isinstance(option, dict)
            ):
                current_character = canonicalize_character_slug(current_character)
            return JSONResponse(
                {
                    "characters": characters,
                    "character_options": character_options,
                    "current": current_character,
                }
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
        "external_llm.auto_approve": {"type": "bool"},
        "agent_team.orchestration_mode": {
            "type": "enum",
            "values": ["standard", "director"],
        },
        "agent_team.delegation_enabled": {"type": "bool"},
        "integrations.spotify.enabled": {"type": "bool"},
        # Team/Subagent/Profile topology is edited atomically through
        # /api/agent-team/config.  The old members/templates/model_groups
        # dotted settings are intentionally not accepted here.
        # 外部送信ポリシーは Agent Team のモデル分担と独立させる。
        "external_model_privacy.mode": {
            "type": "enum",
            "values": ["direct", "protected", "local_only"],
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
        "openai.billing_scope_id": {"type": "str"},
        "search.knowledge_enabled": {"type": "bool"},
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
        # Agent/Tool toggles
        "agents.filesystem.enabled": {"type": "bool"},
        "agents.project_management.enabled": {"type": "bool"},
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
    class_provider_values = {
        "vision": [""] + sorted(MODEL_ROUTING_PROVIDERS - {"claude-cli", "grok-cli", "deepseek"}),
        # 専用client factoryが生成できるproviderだけを許可する。
        # claude/grok はメディア向け互換APIでは使えるが、target factory未対応。
        "clip_ingest": [""] + sorted(AGENT_TEAM_PROVIDERS),
        "video": ["", "mage_vl"],
    }
    for route_class in ("vision", "clip_ingest", "video"):
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

            settings = {
                "external_llm": {
                    "auto_approve": server.config.get(
                        "external_llm.auto_approve", True
                    )
                },
                "agent_team": {
                    **normalize_agent_team_v3(
                        server.config.get("agent_team", {}) or {},
                        global_execution_profiles=server.config.get("execution_profiles"),
                    ),
                    # Read-only presentation metadata is deliberately kept
                    # outside the persisted canonical envelope.
                    "main_effective_route": {
                        "provider": str(server.config.get("llm_provider", "") or ""),
                        "model": str(server.config.get("llm_model", "") or ""),
                    },
                    "capability_catalog": AGENT_TEAM_CAPABILITY_CATALOG,
                },
                "external_model_privacy": {
                    "mode": server.config.get(
                        "external_model_privacy.mode", "direct"
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
                    "openai_model": str(
                        server.config.get("search.openai_model") or ""
                    ).strip(),
                    "knowledge_enabled": server.config.get(
                        "search.knowledge_enabled", False
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
            return JSONResponse({"settings": settings, "schema": ALLOWED_SETTINGS})
        except Exception as e:
            logger.error(f"Failed to get settings: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/agent-team/config")
    async def get_agent_team_config(
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Return the canonical Agent Team schema-v3 envelope.

        Teams own Subagent membership and Team-scoped Execution Profiles.
        Subagents are role/capability definitions only.  Compatibility
        roster/templates/model-groups and user-facing LLM Profiles are omitted.
        """
        section = normalize_agent_team_v3(
            server.config.get("agent_team", {}) or {},
            global_execution_profiles=server.config.get("execution_profiles"),
        )
        envelope = {
            "schema_version": AGENT_TEAM_SCHEMA_VERSION,
            "delegation_enabled": bool(section.get("delegation_enabled", False)),
            "orchestration_mode": str(section.get("orchestration_mode") or "standard"),
            "teams": {
                str(team.get("team_id")): team
                for team in agent_team_v3_teams(server.config)
            },
            "subagents": {
                str(subagent.get("subagent_id")): {
                    **subagent,
                    "effective_route": resolve_agent_team_v3_route(
                        server.config,
                        str(subagent.get("subagent_id") or ""),
                    ),
                }
                for subagent in agent_team_v3_subagents(server.config)
            },
            "main_effective_route": {
                "provider": str(server.config.get("llm_provider", "") or ""),
                "model": str(server.config.get("llm_model", "") or ""),
            },
            "capability_catalog": AGENT_TEAM_CAPABILITY_CATALOG,
        }
        return JSONResponse(
            {
                "success": True,
                "schema_version": AGENT_TEAM_SCHEMA_VERSION,
                "agent_team": envelope,
                "harness": {
                    "independent": True,
                    "settings": public_agent_harness_settings(
                        server.config.get("agent_harness", {}) or {}
                    ),
                },
            }
        )

    @app.put("/api/agent-team/config")
    async def update_agent_team_config(
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Validate and atomically persist the canonical Agent Team v3 graph."""
        if not await server._is_admin_user(request):
            raise HTTPException(status_code=403, detail="Administrator privileges required")
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="JSON payload is required") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Agent Team config must be an object")
        raw = payload.get("agent_team") if isinstance(payload.get("agent_team"), dict) else payload
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="agent_team must be an object")

        # Only canonical schema-v3 fields may be written.  GET-only metadata is
        # accepted for a read/modify/write client but is discarded below.
        writable_fields = {
            "schema_version",
            "delegation_enabled",
            "orchestration_mode",
            "teams",
            "subagents",
        }
        readonly_fields = {
            "main_effective_route",
            "capability_catalog",
            "provider_model",
            "effective_route",
            "llm_profiles",
        }
        unknown = set(raw) - writable_fields - readonly_fields
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported Agent Team field: {sorted(str(item) for item in unknown)[0]}",
            )
        try:
            schema_version = int(raw.get("schema_version", AGENT_TEAM_SCHEMA_VERSION))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="schema_version must be 3") from exc
        if schema_version != AGENT_TEAM_SCHEMA_VERSION:
            raise HTTPException(status_code=400, detail="Only Agent Team schema_version 3 is supported")
        if "delegation_enabled" in raw and not isinstance(raw.get("delegation_enabled"), bool):
            raise HTTPException(status_code=400, detail="delegation_enabled must be boolean")
        orchestration_mode = str(raw.get("orchestration_mode", "standard") or "standard").strip().lower()
        if orchestration_mode not in {"standard", "director"}:
            raise HTTPException(status_code=400, detail="Invalid orchestration_mode")

        teams_raw = raw.get("teams")
        subagents_raw = raw.get("subagents")
        if not isinstance(teams_raw, dict) or not isinstance(subagents_raw, dict):
            raise HTTPException(status_code=400, detail="teams and subagents must be objects")

        id_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        for collection_name, collection in (("team", teams_raw), ("subagent", subagents_raw)):
            for key in collection:
                clean = str(key).strip()
                if not id_pattern.fullmatch(clean):
                    raise HTTPException(status_code=400, detail=f"Invalid {collection_name} id: {key}")

        allowed_team_fields = {
            "team_id", "name", "description", "enabled", "sort_order", "activation",
            "subagent_ids", "execution_profiles",
        }
        allowed_activation_fields = {"mode", "contexts"}
        allowed_subagent_fields = {
            "subagent_id", "name", "description", "instructions", "enabled",
            "capability_ids", "scalable", "default_instances", "max_instances",
            "max_workspace_access", "allow_cli_native_tools",
        }
        readonly_subagent_fields = {"team_ids", "effective_route", "llm_profile_id"}
        allowed_execution_profile_fields = {
            "profile_id", "name", "display_name", "enabled", "default_route", "overrides",
        }
        allowed_execution_route_fields = {
            "inherit_model", "provider", "model", "effort_policy", "effort",
        }
        for key, team in teams_raw.items():
            if not isinstance(team, dict):
                raise HTTPException(status_code=400, detail=f"Invalid Team: {key}")
            extra = set(team) - allowed_team_fields
            if extra:
                raise HTTPException(status_code=400, detail=f"Unsupported Team field for {key}: {sorted(str(item) for item in extra)[0]}")
            if team.get("team_id") is not None and str(team.get("team_id")).strip() != str(key).strip():
                raise HTTPException(status_code=400, detail=f"Team id mismatch: {key}")
            if "enabled" in team and not isinstance(team.get("enabled"), bool):
                raise HTTPException(status_code=400, detail=f"Team enabled must be boolean: {key}")
            if "sort_order" in team and (isinstance(team.get("sort_order"), bool) or not isinstance(team.get("sort_order"), int)):
                raise HTTPException(status_code=400, detail=f"Team sort_order must be integer: {key}")
            activation = team.get("activation", {})
            if not isinstance(activation, dict):
                raise HTTPException(status_code=400, detail=f"Invalid activation for Team: {key}")
            activation_extra = set(activation) - allowed_activation_fields
            if activation_extra:
                raise HTTPException(status_code=400, detail=f"Unsupported activation field for {key}: {sorted(str(item) for item in activation_extra)[0]}")
            mode = str(activation.get("mode") or "always").strip().lower()
            if mode not in {"always", "contextual", "manual"}:
                raise HTTPException(status_code=400, detail=f"Invalid activation mode for {key}")
            contexts = activation.get("contexts", [])
            if not isinstance(contexts, list) or any(not isinstance(item, str) for item in contexts):
                raise HTTPException(status_code=400, detail=f"activation.contexts must be string[]: {key}")
            normalized_contexts = {str(item).strip().lower() for item in contexts if str(item).strip()}
            unknown_contexts = normalized_contexts - set(AGENT_TEAM_CONTEXT_TAGS)
            if unknown_contexts:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown activation context for {key}: {sorted(unknown_contexts)[0]}",
                )
            if mode == "contextual" and not normalized_contexts:
                raise HTTPException(
                    status_code=400,
                    detail=f"Contextual Team requires at least one activation context: {key}",
                )
            refs = team.get("subagent_ids", [])
            if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
                raise HTTPException(status_code=400, detail=f"subagent_ids must be string[]: {key}")
            invalid_refs = [
                str(item).strip()
                for item in refs
                if not id_pattern.fullmatch(str(item).strip())
            ]
            if invalid_refs:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid Subagent reference in {key}: {invalid_refs[0]}",
                )
            execution_profiles = team.get("execution_profiles")
            if execution_profiles is None:
                continue
            if not isinstance(execution_profiles, dict):
                raise HTTPException(status_code=400, detail=f"execution_profiles must be an object: {key}")
            for profile_key, profile in execution_profiles.items():
                if not id_pattern.fullmatch(str(profile_key).strip()):
                    raise HTTPException(status_code=400, detail=f"Invalid execution profile id: {profile_key}")
                if not isinstance(profile, dict):
                    raise HTTPException(status_code=400, detail=f"Invalid Execution Profile: {profile_key}")
                extra_ep = set(profile) - allowed_execution_profile_fields
                if extra_ep:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unsupported Execution Profile field for {profile_key}: {sorted(str(item) for item in extra_ep)[0]}",
                    )
                if profile.get("profile_id") is not None and str(profile.get("profile_id")).strip() != str(profile_key).strip():
                    raise HTTPException(status_code=400, detail=f"Execution Profile id mismatch: {profile_key}")
                if "enabled" in profile and not isinstance(profile.get("enabled"), bool):
                    raise HTTPException(status_code=400, detail=f"Execution Profile enabled must be boolean: {profile_key}")
                default_route = profile.get("default_route")
                if default_route not in (None, "") and not isinstance(default_route, dict):
                    raise HTTPException(status_code=400, detail=f"default_route must be an object: {profile_key}")
                if isinstance(default_route, dict):
                    extra_route = set(default_route) - allowed_execution_route_fields
                    if extra_route:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Unsupported ExecutionRoute field for {profile_key}: {sorted(str(item) for item in extra_route)[0]}",
                        )
                overrides = profile.get("overrides")
                if overrides is None:
                    continue
                if not isinstance(overrides, dict):
                    raise HTTPException(status_code=400, detail=f"overrides must be an object: {profile_key}")
                for override_key, override in overrides.items():
                    if not id_pattern.fullmatch(str(override_key).strip()):
                        raise HTTPException(
                            status_code=400,
                            detail=f"Invalid Execution Profile override key in {profile_key}: {override_key}",
                        )
                    if not isinstance(override, dict):
                        raise HTTPException(
                            status_code=400,
                            detail=f"overrides[{override_key}] must be an object: {profile_key}",
                        )
                    extra_override = set(override) - allowed_execution_route_fields
                    if extra_override:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Unsupported ExecutionRoute field for {profile_key}.overrides[{override_key}]: {sorted(str(item) for item in extra_override)[0]}",
                        )

        for key, subagent in subagents_raw.items():
            if not isinstance(subagent, dict):
                raise HTTPException(status_code=400, detail=f"Invalid Subagent: {key}")
            extra = set(subagent) - allowed_subagent_fields - readonly_subagent_fields
            if extra:
                raise HTTPException(status_code=400, detail=f"Unsupported Subagent field for {key}: {sorted(str(item) for item in extra)[0]}")
            if subagent.get("subagent_id") is not None and str(subagent.get("subagent_id")).strip() != str(key).strip():
                raise HTTPException(status_code=400, detail=f"Subagent id mismatch: {key}")
            if "enabled" in subagent and not isinstance(subagent.get("enabled"), bool):
                raise HTTPException(status_code=400, detail=f"Subagent enabled must be boolean: {key}")
            if "scalable" in subagent and not isinstance(subagent.get("scalable"), bool):
                raise HTTPException(status_code=400, detail=f"Subagent scalable must be boolean: {key}")
            if "allow_cli_native_tools" in subagent and not isinstance(subagent.get("allow_cli_native_tools"), bool):
                raise HTTPException(status_code=400, detail=f"allow_cli_native_tools must be boolean: {key}")
            for field_name in ("default_instances", "max_instances"):
                if field_name in subagent and (isinstance(subagent.get(field_name), bool) or not isinstance(subagent.get(field_name), int)):
                    raise HTTPException(status_code=400, detail=f"{field_name} must be integer: {key}")
            default_instances = subagent.get("default_instances", 1)
            max_instances = subagent.get("max_instances", 1)
            if default_instances < 0 or max_instances < 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"Subagent instance limits must be non-negative/default and positive/max: {key}",
                )
            if default_instances > max_instances:
                raise HTTPException(
                    status_code=400,
                    detail=f"default_instances must be <= max_instances: {key}",
                )
            access = str(subagent.get("max_workspace_access") or "none").strip().lower()
            if access not in {"none", "read", "write"}:
                raise HTTPException(status_code=400, detail=f"Invalid max_workspace_access for {key}")
            capabilities = subagent.get("capability_ids", [])
            if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
                raise HTTPException(status_code=400, detail=f"capability_ids must be string[]: {key}")
            if any(not str(item).strip() for item in capabilities):
                raise HTTPException(status_code=400, detail=f"capability_ids cannot contain empty values: {key}")
            unknown_capabilities = {item.strip() for item in capabilities if item.strip() not in AGENT_TEAM_CAPABILITY_CATALOG}
            if unknown_capabilities:
                raise HTTPException(status_code=400, detail=f"Unknown capability for {key}: {', '.join(sorted(unknown_capabilities))}")

        # Cross-reference validation intentionally happens before normalization:
        # deleting a Subagent requires removing its Team references in this
        # same atomic update, while deleting a Team never deletes Subagents.
        subagent_ids = {str(key).strip() for key in subagents_raw}
        for team_id, team in teams_raw.items():
            unknown_refs = {str(item).strip() for item in team.get("subagent_ids", []) if str(item).strip() not in subagent_ids}
            if unknown_refs:
                raise HTTPException(status_code=400, detail=f"Unknown Subagent reference in {team_id}: {sorted(unknown_refs)[0]}")
            team_member_ids = {
                str(item).strip()
                for item in team.get("subagent_ids", [])
                if str(item).strip()
            }
            execution_profiles = team.get("execution_profiles")
            if not isinstance(execution_profiles, dict):
                continue
            for profile_key, profile in execution_profiles.items():
                if not isinstance(profile, dict):
                    continue
                errors = validate_team_execution_profile(
                    server.config,
                    {
                        **profile,
                        "profile_id": str(profile.get("profile_id") or profile_key).strip(),
                    },
                    source_profile_id=str(profile_key),
                    known_subagent_ids=team_member_ids,
                )
                if errors:
                    raise HTTPException(status_code=400, detail=errors[0])
                default_route = profile.get("default_route")
                if isinstance(default_route, dict):
                    provider = str(default_route.get("provider") or "").strip().lower()
                    if provider and provider not in (MODEL_ROUTING_PROVIDERS | {"routing-profile"}):
                        raise HTTPException(
                            status_code=400,
                            detail=f"Unsupported provider for Execution Profile {profile_key}",
                        )
                    if Features.is_enterprise() and provider == "sglang" and str(default_route.get("model") or "").strip():
                        try:
                            enforce_enterprise_sglang_model(
                                server.config,
                                provider,
                                str(default_route.get("model") or "").strip(),
                            )
                        except (RuntimeError, ValueError) as exc:
                            raise HTTPException(status_code=400, detail=f"Execution Profile {profile_key}: {exc}") from exc
                overrides = profile.get("overrides") if isinstance(profile.get("overrides"), dict) else {}
                for override_key, override in overrides.items():
                    if str(override_key).strip() not in team_member_ids:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Unknown Subagent override in {team_id}/{profile_key}: {override_key}",
                        )
                    if not isinstance(override, dict):
                        continue
                    route_errors = validate_execution_route(
                        server.config,
                        override,
                        label=f"{team_id}.execution_profiles[{profile_key}].overrides[{override_key}]",
                    )
                    if route_errors:
                        raise HTTPException(status_code=400, detail=route_errors[0])
                    provider = str(override.get("provider") or "").strip().lower()
                    if provider and provider not in (MODEL_ROUTING_PROVIDERS | {"routing-profile"}):
                        raise HTTPException(
                            status_code=400,
                            detail=f"Unsupported provider for {team_id}/{profile_key}/{override_key}",
                        )

        candidate = normalize_agent_team_v3(
            {
                "schema_version": AGENT_TEAM_SCHEMA_VERSION,
                "delegation_enabled": bool(raw.get("delegation_enabled", False)),
                "orchestration_mode": orchestration_mode,
                "teams": teams_raw,
                "subagents": subagents_raw,
            },
            global_execution_profiles=server.config.get("execution_profiles"),
        )
        if hasattr(server.config, "save_to_file"):
            if not server.config.save_to_file("agent_team", candidate):
                raise HTTPException(status_code=500, detail="Failed to persist Agent Team config")
        else:
            server.config.set("agent_team", candidate)
        return JSONResponse(
            {
                "success": True,
                "schema_version": AGENT_TEAM_SCHEMA_VERSION,
                "agent_team": candidate,
            }
        )

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
                if not isinstance(value, bool):
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
                if key == "search.openai_model" and (
                    not isinstance(value, str) or not value.strip()
                ):
                    raise ValueError("OpenAI検索モデルを指定してください")
                value = str(value).strip()
                if key == "chatgpt_web.profile_dir" and not value:
                    raise ValueError("ChatGPT会話プロファイルの保存先を指定してください")
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
            elif setting_schema["type"] == "object":
                if not isinstance(value, dict):
                    raise ValueError("Value must be an object")
            if key == "agent_team.orchestration_mode" and value == "director":
                if (
                    str(server.config.get("llm_provider", "") or "")
                    == "routing-profile"
                    and str(server.config.get("llm_model", "") or "")
                    == "free-team"
                ):
                    raise ValueError(
                        "Directorモードは無料Teamルーティングプロファイルでは利用できません"
                    )
                profile_dir = str(
                    server.config.get("chatgpt_web.profile_dir", "") or ""
                ).strip()
                timeout = int(
                    server.config.get(
                        "chatgpt_web.response_timeout_seconds", 900
                    )
                    or 0
                )
                max_rounds = int(
                    server.config.get("chatgpt_web.max_rounds_per_turn", 20)
                    or 0
                )
                if not profile_dir or timeout < 1 or max_rounds < 1:
                    raise ValueError(
                        "Directorモードを有効にする前にChatGPT接続設定を完成させてください"
                    )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Enterprise SGLang is a paired Compose service, not a user-selectable
        # arbitrary OpenAI-compatible endpoint.  Reject route values at the
        # settings boundary as well as in the client factory, so clip ingest
        # and Agent Team cannot persist a model that the server does not serve.
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
            }
            or key.startswith("model_routing.media.")
        ) and value is not False:
            raise HTTPException(
                status_code=403,
                detail="This setting is disabled in the Enterprise profile",
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
            else:
                # Only update in memory
                server.config.set(key, value)

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
            char_config = server.config.get_character_config(character_name)
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

            if auth_enabled is False or is_admin:
                character_manager = CharacterSwitchManager()
                success = character_manager.switch_character(
                    character_name,
                    canonical_character_name,
                )
                if not success:
                    raise HTTPException(
                        status_code=500, detail="Failed to switch character"
                    )
            else:
                success = True

            if success:
                if auth_enabled is False or is_admin:
                    if hasattr(server.config, "save_to_file"):
                        if not server.config.save_to_file(
                            "default_character", character_name
                        ):
                            raise RuntimeError("Failed to persist default_character")
                    else:
                        server.config.set("default_character", character_name)

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
                        session_character = str(
                            getattr(session, "character_name", "") or ""
                        )
                        session_title = str(getattr(session, "title", "") or "")
                        return bool(
                            getattr(session, "is_group_chat", False)
                            or session_character.startswith(("story_", "trpg_"))
                            or session_title.startswith(
                                ("[シナリオ]", "[執筆]", "[TRPG]")
                            )
                        )

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
                        )
                        if not updated:
                            raise RuntimeError("対象セッションを更新できませんでした")
                        session_synchronized = True
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
