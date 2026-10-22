"""Gemini API text-to-speech engine.

Gemini TTS is a cloud provider: the transcript leaves the AoiTalk server and is
sent to ``generativelanguage.googleapis.com``.  Every request therefore goes
through the shared :class:`OutboundPrivacyGateway` (single transaction, no
redirects, no automatic retry), exactly like the other external TTS adapters.

The engine is intentionally provider-shaped rather than model-shaped: the model
is a per-request parameter so one initialized engine can serve characters that
use ``gemini-3.8-flash-tts`` and ``gemini-3.8-flash-lite-tts`` side by side.
"""

from __future__ import annotations

import base64
import io
import os
import re
import wave
from collections.abc import Mapping, Sequence
from typing import Any, Optional

import httpx

from ...services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    PrivacyError,
    get_privacy_policy_context,
)
from .base_engine import BaseTTSEngine

GEMINI_TTS_ENGINE_NAME = "gemini_tts"
GEMINI_TTS_PROVIDER = "gemini"
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com"
GEMINI_API_VERSION = "v1beta"

# Flash-Lite is the high-throughput/low-cost model and is the better default
# for conversational read-aloud.  Flash is selectable globally or per character
# when expressive acting matters more than latency and cost.
GEMINI_TTS_DEFAULT_MODEL = "gemini-3.8-flash-lite-tts"
GEMINI_TTS_MODELS: tuple[dict[str, str], ...] = (
    {
        "id": "gemini-3.8-flash-lite-tts",
        "label": "Gemini 3.8 Flash-Lite TTS",
        "description": "低遅延・低コスト。通常会話と音声会話の既定値",
    },
    {
        "id": "gemini-3.8-flash-tts",
        "label": "Gemini 3.8 Flash TTS",
        "description": "高表現力。演技・感情表現・キャラクター性を重視する場合",
    },
)
GEMINI_TTS_MODEL_IDS = frozenset(model["id"] for model in GEMINI_TTS_MODELS)

GEMINI_TTS_DEFAULT_VOICE = "Kore"
# Prebuilt catalog voices shared by the Gemini TTS models.  Custom voices
# created with the Voices API (``voice_...`` ids or ``voicekey_...`` keys) are
# accepted as free-form identifiers and are not listed here.
GEMINI_TTS_PREBUILT_VOICES: tuple[dict[str, str], ...] = (
    {"id": "Zephyr", "tone": "Bright", "gender": "female"},
    {"id": "Puck", "tone": "Upbeat", "gender": "male"},
    {"id": "Charon", "tone": "Informative", "gender": "male"},
    {"id": "Kore", "tone": "Firm", "gender": "female"},
    {"id": "Fenrir", "tone": "Excitable", "gender": "male"},
    {"id": "Leda", "tone": "Youthful", "gender": "female"},
    {"id": "Orus", "tone": "Firm", "gender": "male"},
    {"id": "Aoede", "tone": "Breezy", "gender": "female"},
    {"id": "Callirrhoe", "tone": "Easy-going", "gender": "female"},
    {"id": "Autonoe", "tone": "Bright", "gender": "female"},
    {"id": "Enceladus", "tone": "Breathy", "gender": "male"},
    {"id": "Iapetus", "tone": "Clear", "gender": "male"},
    {"id": "Umbriel", "tone": "Easy-going", "gender": "male"},
    {"id": "Algieba", "tone": "Smooth", "gender": "male"},
    {"id": "Despina", "tone": "Smooth", "gender": "female"},
    {"id": "Erinome", "tone": "Clear", "gender": "female"},
    {"id": "Algenib", "tone": "Gravelly", "gender": "male"},
    {"id": "Rasalgethi", "tone": "Informative", "gender": "male"},
    {"id": "Laomedeia", "tone": "Upbeat", "gender": "female"},
    {"id": "Achernar", "tone": "Soft", "gender": "female"},
    {"id": "Alnilam", "tone": "Firm", "gender": "male"},
    {"id": "Schedar", "tone": "Even", "gender": "male"},
    {"id": "Gacrux", "tone": "Mature", "gender": "female"},
    {"id": "Pulcherrima", "tone": "Forward", "gender": "female"},
    {"id": "Achird", "tone": "Friendly", "gender": "male"},
    {"id": "Zubenelgenubi", "tone": "Casual", "gender": "male"},
    {"id": "Vindemiatrix", "tone": "Gentle", "gender": "female"},
    {"id": "Sadachbia", "tone": "Lively", "gender": "male"},
    {"id": "Sadaltager", "tone": "Knowledgeable", "gender": "male"},
    {"id": "Sulafat", "tone": "Warm", "gender": "female"},
)
_PREBUILT_VOICE_BY_KEY = {
    voice["id"].casefold(): voice["id"] for voice in GEMINI_TTS_PREBUILT_VOICES
}

GEMINI_TTS_DEFAULT_SAMPLE_RATE = 24000
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CUSTOM_VOICE_RE = re.compile(r"^(?:voice_|voicekey_)[A-Za-z0-9_-]{1,512}$")
_SENTENCE_END_RE = re.compile(r"(?<=[。．！？!?\n])")
_RATE_RE = re.compile(r"rate=(\d+)")


class GeminiTTSError(RuntimeError):
    """Operator-facing Gemini TTS failure without secrets or raw payloads."""


def _gemini_tts_descriptor(*, action: str, destination: str, model: str = ""):
    """Create the canonical outbound descriptor lazily."""

    from ...services.outbound_privacy_service import EgressDescriptor

    return EgressDescriptor(
        action=action,
        transport="httpx",
        destination=destination,
        provider=GEMINI_TTS_PROVIDER,
        tool="tts.gemini",
        model=model,
    )


def resolve_gemini_api_key(config: Any = None) -> str:
    """Resolve the Gemini API key from app config or the environment.

    Keys are never persisted in character settings or source code.
    """

    candidates: list[Any] = []
    if config is not None:
        getter = getattr(config, "get", None)
        if callable(getter):
            try:
                candidates.append(getter("gemini_api_key"))
            except Exception:
                pass
    candidates.extend((os.getenv("GEMINI_API_KEY"), os.getenv("GOOGLE_API_KEY")))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def normalize_gemini_tts_model(value: Any, default: str = GEMINI_TTS_DEFAULT_MODEL) -> str:
    """Return a safe model id; unknown but well-formed ids are allowed."""

    text = str(value or "").strip()
    if text.startswith("models/"):
        text = text[len("models/"):]
    if not text:
        return default
    if not _MODEL_ID_RE.match(text):
        raise GeminiTTSError("Gemini TTSのモデルIDが不正です")
    return text


def normalize_gemini_voice(value: Any, default: str = GEMINI_TTS_DEFAULT_VOICE) -> str:
    """Canonicalize a prebuilt voice name or validate a custom voice id."""

    text = str(value or "").strip()
    if not text:
        return default
    prebuilt = _PREBUILT_VOICE_BY_KEY.get(text.casefold())
    if prebuilt:
        return prebuilt
    if _CUSTOM_VOICE_RE.match(text):
        return text
    # Catalog voices returned by the Voices API may be newer than the static
    # list.  Accept plain speaker-name style identifiers only.
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", text):
        return text
    raise GeminiTTSError("Gemini TTSのボイスIDが不正です")


def split_text_for_tts(text: str, max_chars: int) -> list[str]:
    """Split long text on sentence boundaries into bounded request chunks."""

    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_END_RE.split(text):
        if not sentence:
            continue
        while len(sentence) > max_chars:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.append(sentence[:max_chars].strip())
            sentence = sentence[max_chars:]
        if len(current) + len(sentence) > max_chars and current.strip():
            chunks.append(current.strip())
            current = ""
        current += sentence
    if current.strip():
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def pcm_to_wav(pcm: bytes, sample_rate: int = GEMINI_TTS_DEFAULT_SAMPLE_RATE) -> bytes:
    """Wrap signed 16-bit little-endian mono PCM in a WAV container."""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


def _wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as wav_file:
        if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
            raise GeminiTTSError("Gemini TTSが想定外のWAV形式を返しました")
        return wav_file.readframes(wav_file.getnframes()), wav_file.getframerate()


def extract_audio_pcm(response_json: Any) -> tuple[bytes, int]:
    """Extract PCM from a Gemini Interactions API response.

    Unary TTS responses normally contain a base64-encoded WAV in
    ``output_audio`` (or in the last audio item of ``steps``).  The extractor
    also accepts raw ``audio/l16`` so the response remains safe if the server
    or a future caller requests PCM explicitly.
    """

    if not isinstance(response_json, Mapping):
        raise GeminiTTSError("Gemini TTSの応答形式が不正です")

    audio_items: list[Mapping[str, Any]] = []
    output_audio = response_json.get("output_audio") or response_json.get("outputAudio")
    if isinstance(output_audio, Mapping):
        audio_items.append(output_audio)

    steps = response_json.get("steps")
    if isinstance(steps, list):
        for step in reversed(steps):
            if not isinstance(step, Mapping):
                continue
            content = step.get("content")
            if not isinstance(content, list):
                continue
            for item in reversed(content):
                if isinstance(item, Mapping) and str(item.get("type") or "").lower() == "audio":
                    audio_items.append(item)

    pcm = bytearray()
    sample_rate: int | None = None
    for item in audio_items:
        data = item.get("data")
        if not isinstance(data, str) or not data:
            continue
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception as exc:
            raise GeminiTTSError("Gemini TTSの音声データが不正です") from exc
        mime_type = str(item.get("mime_type") or item.get("mimeType") or "audio/wav").lower()
        if "wav" in mime_type or raw[:4] == b"RIFF":
            chunk, rate = _wav_to_pcm(raw)
        else:
            chunk = raw
            match = _RATE_RE.search(mime_type)
            rate = int(match.group(1)) if match else GEMINI_TTS_DEFAULT_SAMPLE_RATE
        if sample_rate is None:
            sample_rate = rate
        elif sample_rate != rate:
            raise GeminiTTSError("Gemini TTSの音声サンプルレートが一致しません")
        pcm.extend(chunk)

    if not pcm:
        raise GeminiTTSError("Gemini TTSの応答に音声が含まれていません")
    return bytes(pcm), sample_rate or GEMINI_TTS_DEFAULT_SAMPLE_RATE


def _clean_speakers(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    speakers: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("speaker") or item.get("name") or "").strip()
        voice = str(item.get("voice") or item.get("voice_name") or "").strip()
        if name and voice:
            speakers.append({"speaker": name, "voice": normalize_gemini_voice(voice)})
    return speakers


def _interaction_text_content(
    text: str,
    *,
    style: str,
    speakers: Sequence[Mapping[str, str]] | None,
) -> list[dict[str, Any]]:
    """Build the documented Interactions ``input.content`` text items."""

    speaker_names = {
        str(item.get("speaker") or "").strip()
        for item in (speakers or [])
        if isinstance(item, Mapping)
    }
    content: list[dict[str, Any]] = []
    if len(speaker_names) == 2:
        for line in text.splitlines():
            match = re.match(r"^\s*([^:：]+)\s*[:：]\s*(.*)$", line)
            if not match or match.group(1).strip() not in speaker_names:
                content = []
                break
            item: dict[str, Any] = {
                "type": "text",
                "text": match.group(2),
                "annotations": [{"type": "speech_metadata", "speaker": match.group(1).strip()}],
            }
            if style:
                item["annotations"][0]["style"] = style
            content.append(item)
    if content:
        return content

    item = {"type": "text", "text": text}
    if style:
        item["annotations"] = [{"type": "speech_metadata", "style": style}]
    return [item]


def build_interactions_payload(
    model: str,
    text: str,
    *,
    voice: str,
    style: str = "",
    language_code: str = "",
    temperature: Optional[float] = None,
    speakers: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Build the REST ``/v1beta/interactions`` request body for one chunk.

    Gemini 3.8 TTS does not accept the older ``generateContent`` speech
    schema.  It detects the input language automatically, so ``language_code``
    is deliberately not serialized even when the settings UI retains it.
    """

    del language_code, temperature
    clean_speakers = list(speakers or [])
    if len(clean_speakers) == 2:
        speech_config: Any = {
            "mode": "conversational",
            "speakers": [
                {"speaker": speaker["speaker"], "voice": speaker["voice"]}
                for speaker in clean_speakers
            ],
        }
    else:
        speech_config = [{"voice": voice}]
    return {
        "model": model,
        "input": [{
            "type": "user_input",
            "content": _interaction_text_content(text, style=style, speakers=clean_speakers),
        }],
        "response_format": {"type": "audio", "mime_type": "audio/wav"},
        "generation_config": {"speech_config": speech_config},
    }


class GeminiTTSEngine(BaseTTSEngine):
    """Gemini API TTS (``gemini-3.8-flash-tts`` / ``gemini-3.8-flash-lite-tts``)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Optional[str] = None,
        voice: Optional[str] = None,
        style: str = "",
        language_code: str = "",
        temperature: Optional[float] = None,
        timeout: float = 180.0,
        max_chars_per_request: int = 600,
        base_url: str = GEMINI_API_BASE_URL,
        config: Any | None = None,
        privacy_gateway: Any | None = None,
        http_client: Any | None = None,
    ) -> None:
        # Each HTTP call is one privacy transaction; a transparent retry would
        # silently send the transcript twice, so retries are disabled.
        super().__init__(GEMINI_TTS_ENGINE_NAME, retry_count=1)
        self.api_key = (api_key or resolve_gemini_api_key(config)).strip()
        self.default_model = normalize_gemini_tts_model(model)
        self.default_voice = normalize_gemini_voice(voice)
        self.default_style = str(style or "").strip()
        self.default_language_code = str(language_code or "").strip()
        self.default_temperature = temperature
        self.timeout = float(timeout or 180.0)
        # Keep unary provider calls bounded; long Japanese chunks can take
        # several minutes at the 1,500-character setting under real load.
        self.max_chars_per_request = max(100, int(max_chars_per_request or 600))
        self.base_url = str(base_url or GEMINI_API_BASE_URL).rstrip("/")
        self.app_config = config
        self._privacy_gateway = privacy_gateway
        self.client = http_client
        self._owns_client = http_client is None

    # ------------------------------------------------------------------
    # BaseTTSEngine hooks
    # ------------------------------------------------------------------
    async def _initialize_impl(self) -> bool:
        if not self.api_key:
            self.logger.error(
                "Gemini TTS requires GEMINI_API_KEY (or GOOGLE_API_KEY)"
            )
            return False
        if self.client is None:
            self.client = httpx.AsyncClient(
                headers={
                    "x-goog-api-key": self.api_key,
                    "content-type": "application/json",
                },
                timeout=self.timeout,
                # Provider redirects are not part of the API contract; never
                # follow an unreviewed second hop with the transcript.
                follow_redirects=False,
            )
            self._owns_client = True
        return True

    async def _update_voices(self) -> None:
        self._voices = [dict(voice) for voice in GEMINI_TTS_PREBUILT_VOICES]
        self._current_voice = self.default_voice

    async def _cleanup_impl(self) -> None:
        client, self.client = self.client, None
        if client is not None and self._owns_client:
            await client.aclose()

    def _validate_parameters(self, params: dict[str, Any]) -> dict[str, Any]:
        # Gemini has no numeric speed/pitch controls; pacing is expressed via
        # the style prompt.  Keep the generic clamps out of this engine.
        return dict(params)

    # ------------------------------------------------------------------
    # Egress
    # ------------------------------------------------------------------
    def _gateway(self) -> OutboundPrivacyGateway:
        if self._privacy_gateway is not None:
            return self._privacy_gateway
        try:
            from ...services.turn_context import get_turn_context

            turn = get_turn_context()
        except Exception:
            turn = None
        scope = get_privacy_policy_context()
        return OutboundPrivacyGateway(
            self.app_config,
            user_id=str(getattr(turn, "user_id", "") or ""),
            session_id=str(getattr(turn, "session_id", "") or ""),
            session_context=scope.session_context,
            project_metadata=scope.project_metadata,
        )

    async def _execute_external(
        self,
        payload: Any,
        *,
        action: str,
        destination: str,
        model: str,
        sender,
    ) -> Any:
        """Run one Gemini TTS request through the single outbound boundary."""

        gateway = self._gateway()
        execute = getattr(gateway, "execute", None)
        if not callable(execute):
            raise PrivacyError("outbound privacy gateway does not support execution")
        return await execute(
            payload,
            provider=GEMINI_TTS_PROVIDER,
            descriptor=_gemini_tts_descriptor(
                action=action, destination=destination, model=model
            ),
            sender=sender,
            base_url=self.base_url,
            source_kind=action,
            model=model,
        )

    async def _generate_chunk(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        style: str,
        language_code: str,
        temperature: Optional[float],
        speakers: list[dict[str, str]],
    ) -> tuple[bytes, int]:
        endpoint = f"{self.base_url}/{GEMINI_API_VERSION}/interactions"
        payload = build_interactions_payload(
            model,
            text,
            voice=voice,
            style=style,
            language_code=language_code,
            temperature=temperature,
            speakers=speakers,
        )

        async def send(protected_payload: Any) -> Any:
            if not isinstance(protected_payload, Mapping):
                raise RuntimeError("privacy protection returned no protected payload")
            # Only the gateway's final candidate reaches the provider.
            return await self.client.post(endpoint, json=dict(protected_payload))

        response = await self._execute_external(
            payload,
            action="tts.gemini.synthesize",
            destination=endpoint,
            model=model,
            sender=send,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            detail = ""
            try:
                error = response.json().get("error", {})
                detail = str(error.get("status") or error.get("message") or "")[:200]
            except Exception:
                pass
            raise GeminiTTSError(
                f"Gemini TTS APIエラー: HTTP {status}" + (f" {detail}" if detail else "")
            )
        return extract_audio_pcm(response.json())

    async def _synthesize_impl(self, text: str, **params: Any) -> Optional[bytes]:
        if self.client is None:
            raise GeminiTTSError("Gemini TTS engine is not initialized")
        model = normalize_gemini_tts_model(
            params.get("model") or params.get("gemini_model"), self.default_model
        )
        voice = normalize_gemini_voice(
            params.get("voice") or params.get("voice_id") or params.get("voice_name"),
            self.default_voice,
        )
        style = str(params.get("style") or self.default_style or "").strip()
        language_code = str(
            params.get("language_code") or self.default_language_code or ""
        ).strip()
        temperature = params.get("temperature", self.default_temperature)
        if temperature is not None:
            temperature = max(0.0, min(2.0, float(temperature)))
        speakers = _clean_speakers(params.get("speakers"))

        pcm = bytearray()
        sample_rate: int | None = None
        for chunk in split_text_for_tts(text, self.max_chars_per_request):
            chunk_pcm, chunk_sample_rate = await self._generate_chunk(
                chunk,
                model=model,
                voice=voice,
                style=style,
                language_code=language_code,
                temperature=temperature,
                speakers=speakers,
            )
            if sample_rate is None:
                sample_rate = chunk_sample_rate
            elif sample_rate != chunk_sample_rate:
                raise GeminiTTSError("Gemini TTSの分割音声のサンプルレートが一致しません")
            pcm.extend(chunk_pcm)
        if not pcm:
            return None
        return pcm_to_wav(bytes(pcm), sample_rate or GEMINI_TTS_DEFAULT_SAMPLE_RATE)


def gemini_tts_catalog(config: Any = None) -> dict[str, Any]:
    """Static model/voice catalog for settings UIs (no network access)."""

    settings: Mapping[str, Any] = {}
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            candidate = getter("tts_settings", {}) or {}
            if isinstance(candidate, Mapping):
                gemini = candidate.get(GEMINI_TTS_ENGINE_NAME, {})
                if isinstance(gemini, Mapping):
                    settings = gemini
        except Exception:
            settings = {}
    return {
        "engine": GEMINI_TTS_ENGINE_NAME,
        "api_key_configured": bool(resolve_gemini_api_key(config)),
        "default_model": str(settings.get("model") or GEMINI_TTS_DEFAULT_MODEL),
        "default_voice": str(settings.get("voice") or GEMINI_TTS_DEFAULT_VOICE),
        "models": [dict(model) for model in GEMINI_TTS_MODELS],
        "voices": [dict(voice) for voice in GEMINI_TTS_PREBUILT_VOICES],
    }


__all__ = [
    "GEMINI_TTS_DEFAULT_MODEL",
    "GEMINI_TTS_DEFAULT_VOICE",
    "GEMINI_TTS_ENGINE_NAME",
    "GEMINI_TTS_MODELS",
    "GEMINI_TTS_MODEL_IDS",
    "GEMINI_TTS_PREBUILT_VOICES",
    "GeminiTTSEngine",
    "GeminiTTSError",
    "build_interactions_payload",
    "extract_audio_pcm",
    "gemini_tts_catalog",
    "normalize_gemini_tts_model",
    "normalize_gemini_voice",
    "pcm_to_wav",
    "resolve_gemini_api_key",
    "split_text_for_tts",
]
