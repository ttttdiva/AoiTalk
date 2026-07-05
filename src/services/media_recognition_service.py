"""Deterministic pre-processing for attached images and audio."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import time
import wave
from collections import OrderedDict
from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Any

import httpx
from openai import AsyncOpenAI

from src.llm.multimodal import data_url_to_bytes, normalize_image_payloads, openai_content_parts
from src.services.agent_team_service import config_get


MEDIA_RECOGNITION_SYSTEM_PROMPT = """あなたは添付メディアの解析専門モデルです。ユーザーの発言と添付ファイルを受け取ります。
- ユーザーの依頼が添付の処理内容を指定している場合（文字起こし、OCR、要約、構成の説明など）はそれに正確に従ってください。
- 指定がない・添付と無関係な場合: 画像は詳細な客観的記述と可視テキストの完全な転記、音声は完全な文字起こし（話者の区別付き）を返してください。
- 解析結果のみを返し、ユーザーへの回答・意見・挨拶は書かないでください。回答は別のモデルが行います。"""


@dataclass
class RecognitionResult:
    name: str
    sha256: str
    provider: str
    model: str
    engine: str
    result: str = ""
    duration_ms: int = 0
    error: str = ""

    def to_metadata(self) -> dict[str, Any]:
        return asdict(self)


class MediaRecognitionService:
    _cache: "OrderedDict[str, RecognitionResult]" = OrderedDict()
    _cache_limit = 64

    def __init__(self, config: Any):
        self.config = config

    async def recognize_images(
        self,
        user_text: str,
        images: list[dict[str, Any]],
    ) -> list[RecognitionResult]:
        results: list[RecognitionResult] = []
        for image in normalize_image_payloads(images):
            results.append(await self._recognize_one_image(user_text, image))
        return results

    async def recognize_audio(
        self,
        user_text: str,
        audio: dict[str, Any],
    ) -> RecognitionResult:
        started = time.monotonic()
        data_url = str(audio.get("data") or audio.get("dataUrl") or "")
        name = str(audio.get("name") or "audio")
        sha = self._sha256(data_url)
        cached = self._cache_get(sha)
        if cached:
            return cached

        audio_class = config_get(self.config, "model_routing.classes.audio", {}) or {}
        engine = str(audio_class.get("engine") or "speech_recognition").strip()
        provider = str(audio_class.get("provider") or "").strip().lower()
        model = str(audio_class.get("model") or "").strip()
        try:
            if engine == "off":
                raise RuntimeError("音声認識枠が無効です")
            if engine == "speech_recognition":
                text = await asyncio.wait_for(
                    asyncio.to_thread(self._recognize_audio_with_stt, data_url, user_text),
                    timeout=180,
                )
                result = RecognitionResult(
                    name=name,
                    sha256=sha,
                    provider="speech_recognition",
                    model=str(config_get(self.config, "speech_recognition.current_engine", "whisper")),
                    engine=engine,
                    result=text or "",
                    duration_ms=self._elapsed_ms(started),
                )
            else:
                if provider in {"claude", "grok", "codex-cli", "claude-cli", "antigravity-cli"}:
                    raise RuntimeError(f"音声入力に非対応のプロバイダです: {provider}")
                text = await asyncio.wait_for(
                    self._recognize_audio_with_llm(user_text, audio, audio_class),
                    timeout=180,
                )
                result = RecognitionResult(
                    name=name,
                    sha256=sha,
                    provider=provider,
                    model=model,
                    engine=engine,
                    result=text or "",
                    duration_ms=self._elapsed_ms(started),
                )
        except Exception as exc:
            result = RecognitionResult(
                name=name,
                sha256=sha,
                provider=provider or engine,
                model=model,
                engine=engine,
                duration_ms=self._elapsed_ms(started),
                error=str(exc),
            )
        if not result.error:
            self._cache_put(sha, result)
        return result

    async def _recognize_one_image(
        self,
        user_text: str,
        image: dict[str, Any],
    ) -> RecognitionResult:
        started = time.monotonic()
        name = str(image.get("name") or "image")
        sha = self._sha256(str(image.get("data") or ""))
        cached = self._cache_get(sha)
        if cached:
            return cached

        vision = config_get(self.config, "model_routing.classes.vision", {}) or {}
        provider = str(vision.get("provider") or "").strip().lower()
        model = str(vision.get("model") or "").strip()
        try:
            if not provider or not model:
                raise RuntimeError("画像認識モデルが未設定です")
            if provider in {"openai", "openrouter", "grok", "sglang", "openai_compatible_local", "ollama"}:
                text = await asyncio.wait_for(
                    self._recognize_openai_compatible_image(user_text, image, vision),
                    timeout=60,
                )
            elif provider == "gemini":
                text = await asyncio.wait_for(
                    asyncio.to_thread(self._recognize_gemini_image, user_text, image, vision),
                    timeout=60,
                )
            elif provider == "claude":
                text = await asyncio.wait_for(
                    self._recognize_claude_image(user_text, image, vision),
                    timeout=60,
                )
            else:
                raise RuntimeError(f"画像認識に非対応のプロバイダです: {provider}")
            result = RecognitionResult(
                name=name,
                sha256=sha,
                provider=provider,
                model=model,
                engine="vision",
                result=text or "",
                duration_ms=self._elapsed_ms(started),
            )
        except Exception as exc:
            result = RecognitionResult(
                name=name,
                sha256=sha,
                provider=provider,
                model=model,
                engine="vision",
                duration_ms=self._elapsed_ms(started),
                error=str(exc),
            )
        if not result.error:
            self._cache_put(sha, result)
        return result

    def _build_openai_image_messages(self, user_text: str, image: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": MEDIA_RECOGNITION_SYSTEM_PROMPT},
            {"role": "user", "content": openai_content_parts(user_text or "添付画像を解析してください。", [image])},
        ]

    def _build_openai_audio_messages(self, user_text: str, audio: dict[str, Any]) -> list[dict[str, Any]]:
        data_url = str(audio.get("data") or audio.get("dataUrl") or "")
        mime_type, audio_bytes = data_url_to_bytes(data_url)
        encoded = data_url.split(",", 1)[1]
        return [
            {"role": "system", "content": MEDIA_RECOGNITION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text or "添付音声を文字起こししてください。"},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": encoded,
                            "format": self._audio_format(mime_type, audio_bytes),
                        },
                    },
                ],
            },
        ]

    async def _recognize_openai_compatible_image(
        self,
        user_text: str,
        image: dict[str, Any],
        route: dict[str, Any],
    ) -> str:
        client = self._openai_client_for_route(route)
        response = await client.chat.completions.create(
            model=str(route.get("model")),
            messages=self._build_openai_image_messages(user_text, image),
            temperature=0,
            max_tokens=1600,
        )
        return str(response.choices[0].message.content or "")

    async def _recognize_audio_with_llm(
        self,
        user_text: str,
        audio: dict[str, Any],
        route: dict[str, Any],
    ) -> str:
        provider = str(route.get("provider") or "").strip().lower()
        if provider == "gemini":
            return await asyncio.to_thread(self._recognize_gemini_audio, user_text, audio, route)
        client = self._openai_client_for_route(route)
        response = await client.chat.completions.create(
            model=str(route.get("model")),
            messages=self._build_openai_audio_messages(user_text, audio),
            temperature=0,
            max_tokens=2400,
        )
        return str(response.choices[0].message.content or "")

    def _recognize_gemini_image(self, user_text: str, image: dict[str, Any], route: dict[str, Any]) -> str:
        import google.generativeai as genai
        from google.generativeai import protos

        api_key = str(route.get("api_key") or config_get(self.config, "gemini_api_key", "") or "")
        if api_key:
            genai.configure(api_key=api_key)
        mime_type, image_bytes = data_url_to_bytes(str(image.get("data") or ""))
        model = genai.GenerativeModel(str(route.get("model")))
        response = model.generate_content(
            [
                MEDIA_RECOGNITION_SYSTEM_PROMPT,
                user_text or "添付画像を解析してください。",
                protos.Part(inline_data=protos.Blob(mime_type=mime_type, data=image_bytes)),
            ]
        )
        return str(getattr(response, "text", "") or "").strip()

    def _recognize_gemini_audio(self, user_text: str, audio: dict[str, Any], route: dict[str, Any]) -> str:
        import tempfile
        import google.generativeai as genai

        api_key = str(route.get("api_key") or config_get(self.config, "gemini_api_key", "") or "")
        if api_key:
            genai.configure(api_key=api_key)
        mime_type, audio_bytes = data_url_to_bytes(str(audio.get("data") or ""))
        suffix = "." + self._audio_format(mime_type, audio_bytes)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as fp:
            fp.write(audio_bytes)
            fp.flush()
            uploaded = genai.upload_file(path=fp.name)
            model = genai.GenerativeModel(str(route.get("model")))
            response = model.generate_content(
                [MEDIA_RECOGNITION_SYSTEM_PROMPT, user_text or "添付音声を文字起こししてください。", uploaded]
            )
        return str(getattr(response, "text", "") or "").strip()

    async def _recognize_claude_image(self, user_text: str, image: dict[str, Any], route: dict[str, Any]) -> str:
        api_key = str(route.get("api_key") or config_get(self.config, "anthropic_api_key", "") or "")
        if not api_key:
            raise RuntimeError("Anthropic API key is not configured")
        mime_type, image_bytes = data_url_to_bytes(str(image.get("data") or ""))
        import base64

        payload = {
            "model": str(route.get("model")),
            "max_tokens": 1600,
            "system": MEDIA_RECOGNITION_SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text or "添付画像を解析してください。"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            },
                        },
                    ],
                }
            ],
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        return "\n".join(
            str(part.get("text") or "")
            for part in data.get("content", [])
            if isinstance(part, dict)
        ).strip()

    def _recognize_audio_with_stt(self, data_url: str, user_text: str) -> str:
        mime_type, audio_bytes = data_url_to_bytes(data_url)
        frames, sample_rate, channels, sample_width = self._decode_audio_for_stt(
            mime_type,
            audio_bytes,
        )
        from src.audio.manager import SpeechRecognitionManager

        speech_config = config_get(self.config, "speech_recognition", {}) or {}
        engine_name = str(speech_config.get("current_engine") or "whisper")
        manager = SpeechRecognitionManager(engine_name, speech_config)
        return manager.recognize(
            frames,
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            prompt=user_text or None,
        ) or ""

    def _decode_audio_for_stt(self, mime_type: str, audio_bytes: bytes) -> tuple[bytes, int, int, int]:
        """Decode uploaded audio to 16 kHz mono PCM for existing STT engines."""
        suffix = "." + self._audio_format(mime_type, audio_bytes)
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fp:
                fp.write(audio_bytes)
                temp_path = fp.name
            try:
                completed = subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        temp_path,
                        "-f",
                        "s16le",
                        "-acodec",
                        "pcm_s16le",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "pipe:1",
                    ],
                    check=True,
                    capture_output=True,
                )
                if completed.stdout:
                    return completed.stdout, 16000, 1, 2
            finally:
                import os

                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        except (FileNotFoundError, subprocess.CalledProcessError):
            if "wav" not in str(mime_type or "").lower():
                raise RuntimeError("ffmpegで添付音声をデコードできませんでした")

        with wave.open(BytesIO(audio_bytes), "rb") as wav:
            return (
                wav.readframes(wav.getnframes()),
                wav.getframerate(),
                wav.getnchannels(),
                wav.getsampwidth(),
            )

    def _openai_client_for_route(self, route: dict[str, Any]) -> AsyncOpenAI:
        provider = str(route.get("provider") or "").strip().lower()
        base_url = str(route.get("base_url") or "").strip()
        api_key = str(route.get("api_key") or "").strip()
        if provider == "openrouter":
            base_url = base_url or str(config_get(self.config, "openrouter.base_url", "https://openrouter.ai/api/v1"))
            api_key = api_key or str(config_get(self.config, "openrouter_api_key", "") or "")
        elif provider == "grok":
            base_url = base_url or "https://api.x.ai/v1"
            api_key = api_key or str(config_get(self.config, "xai_api_key", "") or "")
        elif provider == "ollama":
            base_url = base_url or str(config_get(self.config, "ollama.base_url", "http://127.0.0.1:11434/v1"))
            api_key = api_key or str(config_get(self.config, "ollama.api_key", "ollama"))
        elif provider == "sglang":
            host = config_get(self.config, "sglang.host", "127.0.0.1")
            port = config_get(self.config, "sglang.port", 30000)
            base_url = base_url or f"http://{host}:{port}/v1"
            api_key = api_key or "dummy"
        elif provider == "openai_compatible_local":
            base_url = base_url or str(config_get(self.config, "openai_compatible_local.base_url", "http://127.0.0.1:8080/v1"))
            api_key = api_key or str(config_get(self.config, "openai_compatible_local.api_key", "dummy"))
        else:
            api_key = api_key or str(config_get(self.config, "openai_api_key", "") or "")
        return AsyncOpenAI(api_key=api_key or "dummy", base_url=base_url or None)

    @classmethod
    def _cache_get(cls, sha: str) -> RecognitionResult | None:
        result = cls._cache.get(sha)
        if result:
            cls._cache.move_to_end(sha)
        return result

    @classmethod
    def _cache_put(cls, sha: str, result: RecognitionResult) -> None:
        cls._cache[sha] = result
        cls._cache.move_to_end(sha)
        while len(cls._cache) > cls._cache_limit:
            cls._cache.popitem(last=False)

    @staticmethod
    def _sha256(value: str) -> str:
        import hashlib

        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _audio_format(mime_type: str, _audio_bytes: bytes) -> str:
        lowered = str(mime_type or "").lower()
        if "mpeg" in lowered or "mp3" in lowered:
            return "mp3"
        if "wav" in lowered:
            return "wav"
        if "webm" in lowered:
            return "webm"
        if "ogg" in lowered:
            return "ogg"
        if "flac" in lowered:
            return "flac"
        if "m4a" in lowered or "mp4" in lowered:
            return "m4a"
        return "wav"
