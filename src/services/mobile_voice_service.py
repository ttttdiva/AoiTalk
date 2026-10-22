"""Server-side native voice I/O; never records or plays on the server itself."""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..features import Features

logger = logging.getLogger(__name__)
MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_RECORDING_SECONDS = 30
MAX_SPEECH_CHARS = 500
MIME_SUFFIXES = {
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
}
MIME_DEMUXERS = {
    "audio/mp4": "mov",
    "audio/m4a": "mov",
    "audio/x-m4a": "mov",
    "audio/wav": "wav",
    "audio/webm": "matroska,webm",
}


class MobileVoiceError(Exception):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


def decode_recording(encoded: str, mime_type: str) -> bytes:
    """Bound both the upload and decoded PCM; do not trust compressed duration."""
    if mime_type not in MIME_SUFFIXES:
        raise MobileVoiceError("対応していない録音形式です。", 422)
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise MobileVoiceError("録音データが不正です。", 422) from exc
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise MobileVoiceError("録音データのサイズが上限を超えているか、空です。", 413)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MobileVoiceError("サーバーに音声デコード用のffmpegがありません。")
    root = Path(__file__).resolve().parents[2] / ".local" / "mobile-voice"
    root.mkdir(parents=True, exist_ok=True)
    # A seekable input is necessary for AAC/M4A written by Android/iOS.
    with tempfile.TemporaryDirectory(prefix="recording-", dir=root) as folder:
        path = Path(folder) / ("input" + MIME_SUFFIXES[mime_type])
        path.write_bytes(audio)
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-f",
                    MIME_DEMUXERS[mime_type],
                    "-i",
                    str(path),
                    "-t",
                    str(MAX_RECORDING_SECONDS + 1),
                    "-vn",
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
                capture_output=True,
                check=True,
                timeout=20,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise MobileVoiceError(
                "録音データを読み取れませんでした。もう一度話してください。", 422
            ) from exc
    pcm = result.stdout
    if len(pcm) > int((MAX_RECORDING_SECONDS + 0.5) * 32000):
        raise MobileVoiceError("1回の録音は30秒までです。", 413)
    if len(pcm) < 6400:
        return b""
    return pcm


class MobileVoiceService:
    """Reuse the configured ASR and character TTS engines, isolated from Web audio.

    Native model calls remain serialized even when the HTTP client disconnects:
    cancelling an await cannot stop a GPU worker thread safely.
    """

    def __init__(self, server: Any):
        self.server = server
        self._asr: Any = None
        self._asr_key: str | None = None
        self._tts: Any = None
        self._tts_key: str | None = None
        self._asr_lock = asyncio.Lock()
        self._tts_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()

    def capabilities(self) -> dict[str, Any]:
        cfg = self.server.config.get("speech_recognition", {}) or {}
        return {
            "input_enabled": Features.voice_input() and bool(shutil.which("ffmpeg")),
            "output_enabled": Features.tts_output(),
            "recognition_engine": str(cfg.get("current_engine") or "whisper"),
            "max_recording_seconds": MAX_RECORDING_SECONDS,
            "max_speech_chars": MAX_SPEECH_CHARS,
        }

    async def _serial(self, lock: asyncio.Lock, operation: Any) -> Any:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=2)
        except asyncio.TimeoutError as exc:
            raise MobileVoiceError(
                "サーバーの音声処理が使用中です。少し後に再試行してください。", 429
            ) from exc

        async def work() -> Any:
            try:
                return await operation()
            finally:
                lock.release()

        task = asyncio.create_task(work())
        self._tasks.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            if not finished.cancelled():
                finished.exception()  # Consume detached failure after a client cancellation.

        task.add_done_callback(done)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=120)
        except asyncio.TimeoutError as exc:
            raise MobileVoiceError(
                "サーバーの音声処理がタイムアウトしました。"
            ) from exc

    async def transcribe(self, encoded: str, mime_type: str) -> str:
        if not Features.voice_input():
            raise MobileVoiceError("このサーバーでは音声入力が無効です。", 403)

        async def work() -> str:
            def recognize() -> str:
                from ..audio.manager import SpeechRecognitionManager

                pcm = decode_recording(encoded, mime_type)
                if not pcm:
                    return ""
                cfg = copy.deepcopy(
                    self.server.config.get("speech_recognition", {}) or {}
                )
                key = json.dumps(cfg, sort_keys=True, default=str)
                if self._asr is None or key != self._asr_key:
                    self._asr = SpeechRecognitionManager(
                        str(cfg.get("current_engine") or "whisper"), cfg
                    )
                    self._asr_key = key
                # Manager's legacy validation cache keys only the byte count, not
                # audio content. Separate utterances must not share that verdict.
                self._asr._validation_cache.clear()
                return str(
                    self._asr.recognize(
                        pcm, sample_rate=16000, channels=1, sample_width=2
                    )
                    or ""
                ).strip()

            return await asyncio.to_thread(recognize)

        try:
            return await self._serial(self._asr_lock, work)
        except MobileVoiceError:
            raise
        except Exception as exc:
            logger.exception("Native voice recognition failed")
            raise MobileVoiceError(
                "サーバーの音声認識に失敗しました。音声認識エンジンの設定を確認してください。"
            ) from exc

    async def synthesize(self, text: str, character_name: str) -> bytes:
        if not Features.tts_output():
            raise MobileVoiceError("このサーバーでは読み上げが無効です。", 403)

        async def work() -> bytes:
            from ..tts.engine_bootstrap import (
                bootstrap_session_tts_manager,
                resolve_character_config_async,
            )
            from ..tts.manager_factory import create_session_tts_manager

            config = self.server.config
            character = await resolve_character_config_async(config, character_name)
            if not character:
                raise MobileVoiceError("キャラクターの音声設定が見つかりません。", 404)
            key = json.dumps([character_name, character], sort_keys=True, default=str)
            if self._tts is None or self._tts_key != key:
                if self._tts is not None:
                    await self._tts.cleanup()
                self._tts = None
                manager = create_session_tts_manager(config)
                try:
                    await bootstrap_session_tts_manager(
                        manager,
                        config=config,
                        character_name=character_name,
                        character_config=character,
                    )
                except BaseException:
                    await manager.cleanup()
                    raise
                self._tts, self._tts_key = manager, key
            audio = await self._tts.synthesize(text, character_name=character_name)
            if not audio:
                raise MobileVoiceError(
                    "読み上げ音声を生成できませんでした。サーバーのTTSエンジンを確認してください。"
                )
            if len(audio) > 20 * 1024 * 1024:
                raise MobileVoiceError("読み上げ音声が長すぎます。", 413)
            return audio

        try:
            return await self._serial(self._tts_lock, work)
        except MobileVoiceError:
            raise
        except Exception as exc:
            logger.exception("Native character speech synthesis failed")
            raise MobileVoiceError(
                "サーバーの読み上げに失敗しました。キャラクターの音声設定とTTSエンジンを確認してください。"
            ) from exc

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._tts is not None:
            await self._tts.cleanup()
            self._tts = None
        self._asr = None
