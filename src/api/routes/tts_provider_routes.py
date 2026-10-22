"""Cloud TTS provider catalog and preview routes.

The character voice settings UI uses these endpoints to show which Gemini TTS
models/voices exist and to audition an unsaved voice selection.  Normal chat,
Live Voice and native mobile read-aloud keep using the character's persisted
voice settings through ``TTSManager``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ...features import Features
from ...tts.engines.gemini_tts_engine import (
    GeminiTTSEngine,
    GeminiTTSError,
    gemini_tts_catalog,
    resolve_gemini_api_key,
)
from ..router_helpers import cookie_auth_dependency

MAX_PREVIEW_CHARS = 300


class GeminiTTSModelOption(BaseModel):
    id: str
    label: str
    description: str


class GeminiTTSVoiceOption(BaseModel):
    id: str
    tone: str
    gender: str


class GeminiTTSCatalogResponse(BaseModel):
    engine: str
    available: bool
    api_key_configured: bool
    default_model: str
    default_voice: str
    models: list[GeminiTTSModelOption]
    voices: list[GeminiTTSVoiceOption]


class GeminiTTSPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_PREVIEW_CHARS)
    model: str | None = Field(default=None, max_length=128)
    voice: str | None = Field(default=None, max_length=600)
    style: str | None = Field(default=None, max_length=1000)
    language_code: str | None = Field(default=None, max_length=16)


def register_tts_provider_routes(app: FastAPI, server: Any) -> None:
    require_auth = cookie_auth_dependency(server._enforce_cookie_auth)

    def settings() -> dict[str, Any]:
        config = getattr(server, "config", None)
        getter = getattr(config, "get", None)
        tts_settings = getter("tts_settings", {}) if callable(getter) else {}
        gemini = tts_settings.get("gemini_tts", {}) if isinstance(tts_settings, dict) else {}
        return gemini if isinstance(gemini, dict) else {}

    @app.get(
        "/api/tts/gemini/catalog",
        response_model=GeminiTTSCatalogResponse,
        operation_id="get_gemini_tts_catalog",
    )
    async def gemini_catalog(_: None = Depends(require_auth)) -> Any:
        catalog = gemini_tts_catalog(getattr(server, "config", None))
        catalog["available"] = bool(
            Features.tts_output() and catalog["api_key_configured"]
        )
        return catalog

    @app.post(
        "/api/tts/gemini/preview",
        operation_id="preview_gemini_tts",
        response_class=Response,
        responses={200: {"content": {"audio/wav": {}}}},
    )
    async def gemini_preview(
        payload: GeminiTTSPreviewRequest,
        _: None = Depends(require_auth),
    ) -> Any:
        if not Features.tts_output():
            raise HTTPException(status_code=403, detail="このサーバーでは読み上げが無効です。")
        config = getattr(server, "config", None)
        if not resolve_gemini_api_key(config):
            raise HTTPException(
                status_code=503,
                detail="GEMINI_API_KEY が設定されていません。",
            )
        defaults = settings()
        engine = GeminiTTSEngine(
            model=defaults.get("model"),
            voice=defaults.get("voice"),
            style=str(defaults.get("style") or ""),
            language_code=str(defaults.get("language_code") or ""),
            timeout=float(defaults.get("timeout", 180.0) or 180.0),
            config=config,
        )
        try:
            if not await engine.initialize():
                raise HTTPException(status_code=503, detail="Gemini TTSを初期化できません。")
            params = {
                key: value
                for key, value in {
                    "model": payload.model,
                    "voice": payload.voice,
                    "style": payload.style,
                    "language_code": payload.language_code,
                }.items()
                if value
            }
            audio = await engine.synthesize(payload.text, **params)
        except HTTPException:
            raise
        except GeminiTTSError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})
        except Exception as exc:
            cause = exc.__cause__
            if isinstance(cause, GeminiTTSError):
                # Application-authored message (status code only, no secrets).
                return JSONResponse(status_code=502, content={"detail": str(cause)})
            return JSONResponse(
                status_code=502,
                content={"detail": "Gemini TTSの試聴生成に失敗しました。"},
            )
        finally:
            await engine.cleanup()
        if not audio:
            return JSONResponse(
                status_code=502,
                content={"detail": "Gemini TTSの試聴生成に失敗しました。"},
            )
        return Response(content=audio, media_type="audio/wav")


__all__ = ["register_tts_provider_routes"]
