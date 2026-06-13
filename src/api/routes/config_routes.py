"""アプリ設定・音声状態・キャラクター系ルート (server.py から移設)"""

import logging
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ..router_helpers import cookie_auth_dependency
from .payloads import SettingsPayload

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
        # (gemini-cli, codex, claude codeなどはモデル名ではなくツール名を表示)
        agent_tool_providers = ["gemini-cli", "codex-cli", "claude-cli"]
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
        "model_sharing.enabled": {"type": "bool"},
        "model_sharing.confirm_prompt": {"type": "bool"},
        "model_sharing.notify": {"type": "bool"},
        "model_sharing.provider": {
            "type": "enum",
            "values": [
                "openai",
                "openrouter",
                "gemini",
                "ollama",
                "openai_compatible_local",
                "sglang",
                "gemini-cli",
                "claude-cli",
                "codex-cli",
            ],
        },
        "model_sharing.model": {"type": "str"},
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
    }

    @app.get("/api/settings")
    async def get_settings(_: None = Depends(require_auth)):
        """Get configurable settings"""
        try:
            settings = {
                "external_llm": {
                    "auto_approve": server.config.get(
                        "external_llm.auto_approve", True
                    )
                },
                "model_sharing": {
                    "enabled": server.config.get(
                        "model_sharing.enabled", False
                    ),
                    "confirm_prompt": server.config.get(
                        "model_sharing.confirm_prompt", True
                    ),
                    "notify": server.config.get("model_sharing.notify", True),
                    "provider": server.config.get(
                        "model_sharing.provider", "openai"
                    ),
                    "model": server.config.get(
                        "model_sharing.model", "gpt-4o"
                    ),
                },
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
            }
            return JSONResponse({"settings": settings, "schema": ALLOWED_SETTINGS})
        except Exception as e:
            logger.error(f"Failed to get settings: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.patch("/api/settings")
    async def update_setting(
        payload: SettingsPayload, _: None = Depends(require_auth)
    ):
        """Update a configuration setting"""
        key = payload.key
        value = payload.value
        persist = payload.persist

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
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

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

            logger.info(f"Setting updated: {key} = {value} (persist={persist})")
            return JSONResponse(
                {"success": True, "key": key, "value": value, "persisted": persist}
            )
        except Exception as e:
            logger.error(f"Failed to update setting: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/character/{character_name}")
    async def switch_character(
        character_name: str, _: None = Depends(require_auth)
    ):
        """Switch to a different character"""
        try:
            # Get character switch manager
            character_manager = CharacterSwitchManager()

            # Try to get character config to validate it exists
            char_config = server.config.get_character_config(character_name)

            # Switch character
            success = character_manager.switch_character(
                character_name,
                character_name.replace(
                    " ", "_"
                ).lower(),  # Convert to yaml filename format
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

                return JSONResponse(
                    {
                        "success": True,
                        "character": character_name,
                        "persisted": True,
                        "message": f"Switched to {character_name}",
                    }
                )
            else:
                raise HTTPException(
                    status_code=500, detail="Failed to switch character"
                )

        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"Character not found: {character_name}"
            )
        except Exception as e:
            logger.error(f"Failed to switch character: {e}")
            raise HTTPException(status_code=500, detail=str(e))
