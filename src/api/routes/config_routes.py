"""アプリ設定・音声状態・キャラクター系ルート (server.py から移設)"""

import logging
import re
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..router_helpers import cookie_auth_dependency
from .payloads import SettingsPayload
from ...services.agent_team_service import (
    AGENT_TEAM_MEMBER_KEYS,
    AGENT_TEAM_PROVIDERS,
    BUILTIN_AGENT_TEAM_MODEL_GROUPS,
    MODEL_ROUTE_CLASS_BY_ROUTE,
    RESERVED_AGENT_TEAM_MODEL_GROUP_IDS,
    SCALABLE_MEMBER_KEYS,
    AGENT_HARNESS_PROVIDERS,
    MODEL_ROUTING_PROVIDERS,
    agent_team_confirm_prompt,
    agent_team_delegation_enabled,
    agent_team_member_for,
    agent_team_member_configured_group_id,
    agent_team_member_mode,
    agent_team_member_requires_external_approval,
    agent_team_member_settings,
    agent_team_notify,
    agent_team_roster,
    resolve_agent_team_member_mode,
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


def register_config_routes(app: FastAPI, server: "WebChatServer") -> None:
    """config / voice_status / characters / settings / character 切替ルートを登録する"""
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    @app.get("/api/config")
    async def get_config():
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

        # Debug logging
        logger.info(
            f"API Config Response - LLM: {llm_model} ({llm_provider}), ASR: {speech_engine} ({speech_model})"
        )

        return JSONResponse(
            {
                "character_name": server.character_name,
                "max_history": server.manager.max_history,
                "llm_model": llm_model,
                "llm_provider": llm_provider,
                "asr_engine": speech_engine,
                "asr_model": speech_model,
                "session_id": session_id,
            }
        )

    @app.get("/api/voice_status")
    async def get_voice_status():
        """Get voice recognition status"""
        return JSONResponse(
            {
                "ready": server.voice_recognition_ready,
                "rms": server.current_rms,
                "recording": server.is_recording,
            }
        )

    @app.get("/api/characters")
    async def get_characters():
        """Get list of available characters"""
        try:
            characters = server.config.get_available_characters()
            return JSONResponse(
                {"characters": characters, "current": server.character_name}
            )
        except Exception as e:
            logger.error(f"Failed to get characters: {e}")
            return JSONResponse(
                {"characters": [], "current": server.character_name, "error": str(e)},
                status_code=500,
            )

    # ── Settings API Endpoints ──────────────────────────────────────────
    # Allowed settings that can be modified via WebUI
    ALLOWED_SETTINGS = {
        "external_llm.auto_approve": {"type": "bool"},
        "agent_team.delegation_enabled": {"type": "bool"},
        "agent_team.confirm_prompt": {"type": "bool"},
        "agent_team.notify": {"type": "bool"},
        "agent_team.redaction_terms": {"type": "str_list"},
        "agent_team.strategy": {
            "type": "enum",
            "values": ["adaptive", "fanout", "judge"],
        },
        "agent_team.model_groups": {"type": "object"},
        "agent_team.members": {"type": "object"},
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
        # Agent/Tool toggles
        "agents.filesystem.enabled": {"type": "bool"},
        "agents.project_management.enabled": {"type": "bool"},
        "mcp_enabled": {"type": "bool"},
        "agents.spotify.enabled": {"type": "bool"},
        "spotify.enabled": {"type": "bool"},
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
    }
    class_provider_values = {
        "vision": [""] + sorted(MODEL_ROUTING_PROVIDERS - {"claude-cli", "grok-cli"}),
    }
    for route_class in ("vision",):
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
    for member_key in sorted(AGENT_TEAM_MEMBER_KEYS):
        ALLOWED_SETTINGS[f"agent_team.members.{member_key}.enabled"] = {"type": "bool"}
        ALLOWED_SETTINGS[f"agent_team.members.{member_key}.group_id"] = {"type": "str"}
        ALLOWED_SETTINGS[f"agent_team.members.{member_key}.override.effort_policy"] = {
            "type": "enum", "values": ["same", "lower", "explicit", "default"]
        }
        ALLOWED_SETTINGS[f"agent_team.members.{member_key}.override.effort"] = {"type": "str"}
        provider_values = (
            sorted(AGENT_HARNESS_PROVIDERS)
            if member_key == "agent_harness"
            else sorted(AGENT_TEAM_PROVIDERS)
        )
        ALLOWED_SETTINGS[f"model_routing.overrides.{member_key}.provider"] = {
            "type": "enum",
            "values": [""] + provider_values,
        }
        for field in ("provider", "model", "runner"):
            ALLOWED_SETTINGS[f"agent_team.members.{member_key}.override.{field}"] = {"type": "str"}
        for field in ("model", "mode", "reasoning_effort", "runner"):
            ALLOWED_SETTINGS[f"model_routing.overrides.{member_key}.{field}"] = {
                "type": "str"
            }
        ALLOWED_SETTINGS[f"model_routing.overrides.{member_key}.default_instances"] = {
            "type": "int",
            "min": 0,
            "max": 32,
        }
        ALLOWED_SETTINGS[f"model_routing.overrides.{member_key}.max_instances"] = {
            "type": "int",
            "min": 1,
            "max": 32,
        }

    @app.get("/api/settings")
    async def get_settings(_: None = Depends(require_auth)):
        """Get configurable settings"""
        try:
            def _member_payload(member_key: str) -> dict:
                member = agent_team_member_settings(server.config, member_key)
                target = agent_team_member_for(server.config, member_key)
                raw = server.config.get(f"agent_team.members.{member_key}", {}) or {}
                return {
                    "enabled": bool(raw.get("enabled", member.get("enabled", False))),
                    "group_id": agent_team_member_configured_group_id(
                        server.config,
                        member_key,
                    ),
                    "override": dict(raw.get("override") or {}),
                    "effective_provider": str((target or {}).get("provider") or member.get("provider") or ""),
                    "effective_model": str((target or {}).get("model") or member.get("model") or ""),
                    "effective_effort": resolve_agent_team_member_mode(
                        server.config,
                        member_key=member_key,
                        provider=str((target or {}).get("provider") or member.get("provider") or ""),
                        model=str((target or {}).get("model") or member.get("model") or ""),
                    ),
                    "provider": str(member.get("provider") or ""),
                    "model": str(member.get("model") or ""),
                    "mode": agent_team_member_mode(
                        server.config,
                        member_key,
                        "medium",
                    ),
                    "reasoning_effort": agent_team_member_mode(
                        server.config,
                        member_key,
                        "medium",
                    ),
                    "external": agent_team_member_requires_external_approval(
                        target or {
                            "provider": str(member.get("provider") or ""),
                            "model": str(member.get("model") or ""),
                        }
                    ),
                    "label": str(member.get("label") or ""),
                    "role": str(member.get("role") or member_key),
                    "runner": str(member.get("runner") or ""),
                    "scalable": bool(member.get("scalable", False)),
                    "default_instances": int(member.get("default_instances") or 0),
                    "max_instances": int(member.get("max_instances") or 1),
                    "tools": list(member.get("tools") or []),
                }

            settings = {
                "external_llm": {
                    "auto_approve": server.config.get(
                        "external_llm.auto_approve", True
                    )
                },
                "agent_team": {
                    "delegation_enabled": agent_team_delegation_enabled(server.config),
                    "confirm_prompt": agent_team_confirm_prompt(server.config),
                    "notify": agent_team_notify(server.config),
                    "redaction_terms": server.config.get(
                        "agent_team.redaction_terms", []
                    ),
                    "strategy": server.config.get(
                        "agent_team.strategy", "adaptive"
                    ),
                    "member_settings_initialized": server.config.get(
                        "agent_team.member_settings_initialized", False
                    ),
                    "model_groups": server.config.get("agent_team.model_groups", {}),
                    "members": {
                        member_key: _member_payload(member_key)
                        for member_key in sorted(AGENT_TEAM_MEMBER_KEYS)
                    },
                    "roster": agent_team_roster(server.config),
                },
                "model_routing": server.config.get("model_routing", {}),
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
                    "mcp": {"enabled": server.config.get("mcp_enabled", True)},
                    "spotify": {
                        "enabled": server.config.get("agents.spotify.enabled", True)
                    },
                },
                "spotify": {"enabled": server.config.get("spotify.enabled", True)},
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

        if key.startswith("tts.yomi_linter.") and not await server._is_admin_user(request):
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
                value = str(value).strip()
            elif setting_schema["type"] == "int":
                value = int(value)
                if "min" in setting_schema and value < setting_schema["min"]:
                    raise ValueError(f"Value must be >= {setting_schema['min']}")
                if "max" in setting_schema and value > setting_schema["max"]:
                    raise ValueError(f"Value must be <= {setting_schema['max']}")
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
                if key == "agent_team.model_groups":
                    missing_builtin = set(BUILTIN_AGENT_TEAM_MODEL_GROUPS) - set(value)
                    if missing_builtin:
                        raise ValueError(
                            f"Built-in model groups cannot be deleted: {sorted(missing_builtin)}"
                        )
                    for group_id, group in value.items():
                        clean_group_id = str(group_id).strip()
                        if not clean_group_id or not isinstance(group, dict):
                            raise ValueError("Each model group must be an object with a non-empty id")
                        if not re.fullmatch(r"[A-Za-z0-9_-]+", clean_group_id):
                            raise ValueError(f"Invalid model group id: {clean_group_id}")
                        if (
                            clean_group_id in RESERVED_AGENT_TEAM_MODEL_GROUP_IDS
                            and clean_group_id not in BUILTIN_AGENT_TEAM_MODEL_GROUPS
                        ):
                            raise ValueError(f"Reserved model group id: {clean_group_id}")
                        builtin = BUILTIN_AGENT_TEAM_MODEL_GROUPS.get(clean_group_id)
                        if builtin and str(group.get("name") or "") != builtin["name"]:
                            raise ValueError(
                                f"Built-in model group name cannot be changed: {clean_group_id}"
                            )
                        provider = str(group.get("provider") or "")
                        if provider and provider not in AGENT_TEAM_PROVIDERS:
                            raise ValueError(f"Invalid Agent Team provider: {provider}")
                        model = str(group.get("model") or "").strip()
                        if provider and not model:
                            raise ValueError(
                                f"Model group {clean_group_id} must select a model for its provider"
                            )
                        effort_policy = str(group.get("effort_policy") or "same")
                        if effort_policy not in {"same", "lower", "explicit", "default"}:
                            raise ValueError("Invalid effort policy")
                        if effort_policy == "explicit":
                            from ...services.llm_model_catalog import (
                                reasoning_effort_options_for_model,
                            )

                            effective_provider = provider or str(
                                server.config.get("llm_provider", "") or ""
                            )
                            effective_model = model or str(
                                server.config.get("llm_model", "") or ""
                            )
                            options = reasoning_effort_options_for_model(
                                effective_provider,
                                effective_model,
                            )
                            effort = str(group.get("effort") or "").strip()
                            if not options:
                                raise ValueError(
                                    f"Model group {clean_group_id} uses a model without effort support"
                                )
                            if effort not in options:
                                raise ValueError(
                                    f"Invalid effort for model group {clean_group_id}: {effort or '(not selected)'}"
                                )
                elif key == "agent_team.members":
                    unknown = set(value) - AGENT_TEAM_MEMBER_KEYS
                    if unknown:
                        raise ValueError(f"Unknown Agent Team members: {sorted(unknown)}")
                    for member_key, member in value.items():
                        if not isinstance(member, dict) or not isinstance(member.get("enabled"), bool):
                            raise ValueError(f"Invalid Agent Team member: {member_key}")
                        group_id = str(member.get("group_id") or "").strip()
                        if group_id == "auto" and member_key not in SCALABLE_MEMBER_KEYS:
                            raise ValueError(
                                f"Automatic model grouping is not supported for {member_key}"
                            )
                        model_groups = server.config.get("agent_team.model_groups", {}) or {}
                        if group_id and group_id != "auto" and group_id not in model_groups:
                            raise ValueError(
                                f"Unknown model group for {member_key}: {group_id}"
                            )
                        override = member.get("override") or {}
                        if not isinstance(override, dict):
                            raise ValueError(f"Invalid member override: {member_key}")
                        provider = str(override.get("provider") or "")
                        allowed = AGENT_HARNESS_PROVIDERS if member_key == "agent_harness" else AGENT_TEAM_PROVIDERS
                        if provider and provider not in allowed:
                            raise ValueError(f"Invalid provider for {member_key}: {provider}")
                        if str(override.get("effort_policy") or "same") not in {"same", "lower", "explicit", "default"}:
                            raise ValueError(f"Invalid effort policy for {member_key}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # Apply the setting
        try:
            if key == "agent_team.delegation_enabled" and value and not server.config.get(
                "agent_team.member_settings_initialized", False
            ):
                initial_members = {
                    member_key: {
                        "enabled": member_key != "advanced_reasoning",
                        "group_id": MODEL_ROUTE_CLASS_BY_ROUTE.get(member_key, ""),
                        "override": {},
                    }
                    for member_key in sorted(AGENT_TEAM_MEMBER_KEYS)
                }
                # Harness cannot safely inherit an arbitrary main provider.
                harness_provider = str(server.config.get("agent_team.members.agent_harness.override.provider", "") or "")
                if harness_provider not in AGENT_HARNESS_PROVIDERS:
                    harness_provider = "claude-cli" if server.config.get("agent_harness.codex.runner", "") == "claude_code" else "codex-cli"
                harness_model_key = "agent_harness.claude.model" if harness_provider == "claude-cli" else "agent_harness.codex.model"
                initial_members["agent_harness"]["override"].update({
                    "provider": harness_provider,
                    "model": str(server.config.get(harness_model_key, "") or ("sonnet" if harness_provider == "claude-cli" else "gpt-5-codex")),
                    "runner": "claude_code" if harness_provider == "claude-cli" else "codex_exec",
                })
                setter = server.config.save_to_file if persist else server.config.set
                setter("agent_team.members", initial_members)
                setter("agent_team.member_settings_initialized", True)
            if key == "agent_team.members":
                (server.config.save_to_file if persist else server.config.set)(
                    "agent_team.member_settings_initialized", True
                )
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

            logger.info(f"Setting updated: {key} = {value} (persist={persist})")
            return JSONResponse(
                {"success": True, "key": key, "value": value, "persisted": persist}
            )
        except Exception as e:
            logger.error(f"Failed to update setting: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/character/{character_name}")
    async def switch_character(
        character_name: str,
        request: Request,
        _: None = Depends(require_auth),
    ):
        """Switch to a different character"""
        try:
            # Get character switch manager
            character_manager = CharacterSwitchManager()

            # Try to get character config to validate it exists
            char_config = server.config.get_character_config(character_name)
            db_character = char_config.get("_db_character", {})
            canonical_character_name = (
                str(db_character.get("slug") or character_name).strip()
            )

            # Switch character
            success = character_manager.switch_character(
                character_name,
                canonical_character_name,
            )

            if success:
                if hasattr(server.config, "save_to_file"):
                    if not server.config.save_to_file(
                        "default_character", character_name
                    ):
                        raise RuntimeError("Failed to persist default_character")
                else:
                    server.config.set("default_character", character_name)

                # Update server's character name
                server.character_name = character_name

                # The selected header character also becomes the character for
                # the currently active normal chat session. Scenario/group
                # sessions remain isolated from the global character switch.
                session_synchronized = False
                session_sync_required = False
                try:
                    user_info = await server._get_user_info_from_request(request)
                    auth_enabled = bool(getattr(server, "auth_enabled", True))
                    if user_info is None and auth_enabled:
                        raise PermissionError(
                            "認証済みユーザーを解決できないため、セッションを同期できません"
                        )
                    user_id = str(
                        (user_info or {}).get("id")
                        or ("default_user" if not auth_enabled else "")
                    ).strip()
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
                            or session_character.startswith(
                                (
                                    "scenario_roleplay:",
                                    "scenario_",
                                    "trpg_room_",
                                )
                            )
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
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"Character not found: {character_name}"
            )
        except Exception as e:
            logger.error(f"Failed to switch character: {e}")
            raise HTTPException(status_code=500, detail=str(e))
