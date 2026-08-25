"""Mage-VL video recognition adapter.

Mage-VL's official online inference path is an OpenAI-compatible SGLang
server.  AoiTalk keeps that server outside of the web request process: it can
either connect to an already running server or start one on the first video
request.  The model itself is therefore never imported or downloaded during a
normal application import/startup.
"""

from __future__ import annotations

import asyncio
import copy
import base64
import inspect
import io
import json
import logging
import os
import shlex
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from PIL import Image

from src.llm.conversation_context import normalize_usage
from src.services.agent_team_service import config_get
from src.services.turn_context import get_turn_context
from src.services.outbound_privacy_service import (
    OutboundPrivacyGateway,
    effective_privacy_mode,
    get_privacy_policy_context,
    privacy_config,
)


logger = logging.getLogger(__name__)


def persist_usage_sync(*args: Any, **kwargs: Any) -> bool:
    """Lazy usage persistence avoids an import cycle during service startup."""

    from src.llm.conversation_context import persist_usage_sync as _persist

    return bool(_persist(*args, **kwargs))

DEFAULT_MAGE_VL_MODEL = "microsoft/Mage-VL"
DEFAULT_MAGE_VL_BASE_URL = "http://127.0.0.1:30000/v1"
DEFAULT_NUM_FRAMES = 32
DEFAULT_MAX_PIXELS = 150_000
DEFAULT_MAX_NEW_TOKENS = 256
DEFAULT_INFERENCE_TIMEOUT_SECONDS = 600

ProgressCallback = Callable[[str, str], Awaitable[None] | None]


class MageVLUnavailableError(RuntimeError):
    """Raised when the configured Mage-VL server cannot be reached or started."""


@dataclass(frozen=True)
class MageVLRecognition:
    """The result and runtime information returned by one Mage-VL request."""

    result: str
    load_wait_ms: int = 0
    num_frames: int = 0
    duration_seconds: float | None = None


@dataclass(frozen=True)
class _UsageContextSnapshot:
    """Immutable identity snapshot for one direct Mage-VL request."""

    current_session_id: Any = None
    current_project_id: Any = None
    character_name: Any = None
    user_id: Any = None

    def _get_session_user_id(self) -> Any:
        return self.user_id


class MageVLService:
    """Own or connect to one Mage-VL SGLang server for a Config instance."""

    def __init__(self, config: Any, usage_context: Any = None):
        self.config = config
        self._usage_context = usage_context
        self._recorded_usage_responses: list[Any] = []
        self._ready_lock = asyncio.Lock()
        self._process: subprocess.Popen[Any] | None = None
        self._state = "unloaded"
        self._last_error = ""
        self._last_load_wait_ms = 0
        self._last_probe_error = ""
        self._privacy_gateway = OutboundPrivacyGateway(
            config,
            user_id=str(self._context_value(usage_context, "user_id") or ""),
            session_id=str(
                self._context_value(usage_context, "current_session_id", "session_id")
                or ""
            ),
        )
        self._sync_request_context(usage_context)

    @staticmethod
    def _context_value(context: Any, name: str, *aliases: str) -> Any:
        keys = (name, *aliases)
        if isinstance(context, Mapping):
            for key in keys:
                value = context.get(key)
                if value is not None:
                    return value
        for key in keys:
            value = getattr(context, key, None)
            if value is not None:
                return value
        return None

    def _sync_request_context(self, context: Any = None) -> None:
        """Synchronize singleton identity and effective privacy policy per turn."""

        if context is not None:
            self._usage_context = context
        candidate = context if context is not None else self._usage_context
        if context is None:
            try:
                turn = get_turn_context()
            except Exception:
                turn = None
            if turn is not None and any(
                getattr(turn, field, None)
                for field in ("user_id", "session_id", "project_id")
            ):
                candidate = turn
        user_id = self._context_value(candidate, "user_id", "session_user_id")
        if user_id is None:
            getter = getattr(candidate, "_get_session_user_id", None)
            if callable(getter):
                try:
                    user_id = getter()
                except Exception:
                    user_id = None
        session_id = self._context_value(candidate, "current_session_id", "session_id")
        previous_identity = (
            self._privacy_gateway.user_id,
            self._privacy_gateway.session_id,
        )
        self._privacy_gateway.user_id = str(user_id or "")
        self._privacy_gateway.session_id = str(session_id or "")
        if previous_identity != (
            self._privacy_gateway.user_id,
            self._privacy_gateway.session_id,
        ):
            self._privacy_gateway._raw_to_alias.clear()
            self._privacy_gateway._alias_to_raw.clear()

        inherited = get_privacy_policy_context()
        session_policy = self._context_value(candidate, "session_context", "privacy_context")
        project_metadata = self._context_value(
            candidate,
            "project_metadata",
            "project_metadata_context",
        )
        if not isinstance(session_policy, Mapping):
            session_policy = inherited.session_context
        if not isinstance(project_metadata, Mapping):
            project_metadata = inherited.project_metadata
        privacy_mode = self._context_value(candidate, "privacy_mode")
        if privacy_mode is not None:
            session_policy = dict(session_policy or {})
            session_policy["privacy_mode"] = str(privacy_mode or "")
        self._privacy_gateway.update_policy_context(
            session_context=dict(session_policy or {}),
            project_metadata=dict(project_metadata or {}),
        )
        settings = privacy_config(self.config)
        self._privacy_gateway.settings = settings.__class__(
            **{
                **settings.__dict__,
                "mode": effective_privacy_mode(
                    self.config,
                    session_context=self._privacy_gateway.session_context,
                    project_metadata=self._privacy_gateway.project_metadata,
                ),
            }
        )

    def set_request_context(self, usage_context: Any = None) -> None:
        """Update per-turn context without replacing the process-shared service."""

        self._sync_request_context(usage_context)

    def _request_gateway_snapshot(self, usage_context: Any = None) -> OutboundPrivacyGateway:
        """Capture a private gateway before the first await of one video turn.

        Mage-VL is a process singleton.  Mutating its gateway and then
        awaiting frame extraction allowed another user's turn to replace the
        identity/alias map before transport.  A shallow copy keeps settings
        and callbacks while isolating reversible alias state.
        """

        self._sync_request_context(usage_context)
        gateway = copy.copy(self._privacy_gateway)
        gateway._raw_to_alias = dict(self._privacy_gateway._raw_to_alias)
        gateway._alias_to_raw = dict(self._privacy_gateway._alias_to_raw)
        gateway._counters = dict(self._privacy_gateway._counters)
        gateway.audit = []
        return gateway

    def _usage_client(self, context: Any = None) -> Any:
        """Build a tracking context while preserving turn identity."""

        # Do not retain a mutable request client on the process-shared Mage-VL
        # singleton.  Snapshot all identity fields into a fresh proxy for this
        # invocation so concurrent videos cannot cross-attribute usage rows.
        try:
            turn = get_turn_context()
        except Exception:
            turn = None
        candidate = context if context is not None else self._usage_context
        if context is None and turn is not None and any(
            getattr(turn, field, None)
            for field in ("user_id", "session_id", "project_id")
        ):
            candidate = turn

        def _value(name: str, default: Any = None) -> Any:
            if isinstance(candidate, Mapping):
                value = candidate.get(name)
                if value is not None:
                    return value
            value = getattr(candidate, name, None)
            if value is not None:
                return value
            return getattr(turn, name, default) if turn is not None else default

        user_id = _value("user_id")
        if user_id is None:
            getter = getattr(candidate, "_get_session_user_id", None)
            if callable(getter):
                try:
                    user_id = getter()
                except Exception:
                    user_id = None
        return _UsageContextSnapshot(
            current_session_id=_value("current_session_id", _value("session_id")),
            current_project_id=_value("current_project_id", _value("project_id")),
            character_name=_value("character_name"),
            user_id=user_id,
        )

    def _mark_usage_recorded(self, response: Any) -> bool:
        try:
            if getattr(response, "_aoitalk_usage_recorded", False):
                return True
            object.__setattr__(response, "_aoitalk_usage_recorded", True)
            return False
        except Exception:
            if any(item is response for item in self._recorded_usage_responses):
                return True
            self._recorded_usage_responses.append(response)
            del self._recorded_usage_responses[:-16]
            return False

    def _record_usage(
        self,
        response: Any,
        *,
        started: float | None = None,
        latency_ms: int | None = None,
        usage_context: Any = None,
    ) -> bool:
        """Record one successful Mage-VL response when usage is reported."""

        try:
            raw_usage = (
                response.get("usage")
                if isinstance(response, Mapping)
                else getattr(response, "usage", None)
            )
            if raw_usage is None:
                return False
            usage = normalize_usage(
                raw_usage,
                provider="mage_vl",
                resolved_model=(
                    response.get("model")
                    if isinstance(response, Mapping)
                    else getattr(response, "model", None)
                ),
            )
            if usage.get("input_tokens") is None and usage.get("output_tokens") is None:
                return False
            if self._mark_usage_recorded(response):
                return False
            persist_usage_sync(
                self._usage_client(usage_context),
                provider="mage_vl",
                model=self.model(),
                usage=usage,
                request_type="vision",
                latency_ms=(
                    max(0, int(latency_ms))
                    if latency_ms is not None
                    else (
                        max(0, int((time.monotonic() - started) * 1000))
                        if started is not None
                        else 0
                    )
                ),
                is_streaming=False,
            )
            return True
        except Exception:
            logger.debug("Mage-VL usageの記録に失敗しました", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Configuration and status
    # ------------------------------------------------------------------
    def _settings(self) -> dict[str, Any]:
        raw = config_get(self.config, "mage_vl", {}) or {}
        return dict(raw) if isinstance(raw, dict) else {}

    def _video_route(self) -> dict[str, Any]:
        raw = config_get(self.config, "model_routing.classes.video", {}) or {}
        return dict(raw) if isinstance(raw, dict) else {}

    def enabled(self) -> bool:
        return bool(self._settings().get("enabled", True))

    def managed(self) -> bool:
        return bool(self._settings().get("managed", True))

    def preload_on_start(self) -> bool:
        return bool(self._settings().get("preload_on_start", False))

    def inference_timeout_seconds(self) -> float:
        try:
            value = float(
                self._settings().get(
                    "inference_timeout_seconds",
                    DEFAULT_INFERENCE_TIMEOUT_SECONDS,
                )
            )
        except (TypeError, ValueError):
            value = DEFAULT_INFERENCE_TIMEOUT_SECONDS
        return max(1.0, value)

    def model(self) -> str:
        route = self._video_route()
        settings = self._settings()
        return str(
            route.get("model")
            or settings.get("model")
            or DEFAULT_MAGE_VL_MODEL
        ).strip()

    def base_url(self) -> str:
        route = self._video_route()
        settings = self._settings()
        value = str(
            route.get("base_url")
            or settings.get("base_url")
            or DEFAULT_MAGE_VL_BASE_URL
        ).strip()
        if not value:
            return DEFAULT_MAGE_VL_BASE_URL
        return value.rstrip("/")

    def api_key(self) -> str:
        route = self._video_route()
        settings = self._settings()
        return str(route.get("api_key") or settings.get("api_key") or "dummy").strip() or "dummy"

    def status(self) -> dict[str, Any]:
        process = self._process
        return {
            "state": self._state,
            "enabled": self.enabled(),
            "managed": self.managed(),
            "preload_on_start": self.preload_on_start(),
            "model": self.model(),
            "base_url": self.base_url(),
            "owned_process": bool(process and process.poll() is None),
            "pid": process.pid if process and process.poll() is None else None,
            "load_wait_ms": self._last_load_wait_ms,
            "error": self._last_error,
        }

    async def preload_if_configured(self) -> None:
        """Warm the configured server only when explicitly requested."""

        if not self.enabled() or not self.preload_on_start():
            return
        # A preload may run before any video request has materialized.  Apply
        # the same provider/privacy gate first so local_only cannot probe or
        # start a remote Mage-VL endpoint during application startup.
        self._sync_request_context()
        self._privacy_gateway.ensure_provider_allowed(
            "mage_vl",
            base_url=self.base_url(),
        )
        await self.ensure_ready()

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------
    async def _notify(
        self,
        callback: ProgressCallback | None,
        status: str,
        message: str,
    ) -> None:
        if callback is None:
            return
        value = callback(status, message)
        if inspect.isawaitable(value):
            await value

    async def ensure_ready(self, progress_callback: ProgressCallback | None = None) -> int:
        """Return the wait time after ensuring the endpoint is ready.

        The lock covers both the probe and process creation, so concurrent
        first video requests cannot launch duplicate SGLang processes.
        """

        self._sync_request_context()
        self._privacy_gateway.ensure_provider_allowed(
            "mage_vl",
            base_url=self.base_url(),
        )
        started = time.monotonic()
        async with self._ready_lock:
            if not self.enabled():
                self._state = "error"
                self._last_error = "Mage-VL動画認識が無効です"
                raise MageVLUnavailableError(self._last_error)

            if await self._probe():
                self._state = "ready"
                self._last_error = ""
                wait_ms = int((time.monotonic() - started) * 1000)
                self._last_load_wait_ms = wait_ms
                await self._notify(progress_callback, "ready", "動画認識モデルの準備が完了しました")
                return wait_ms

            if not self.managed():
                detail = self._last_probe_error or "接続できませんでした"
                self._state = "error"
                self._last_error = f"Mage-VLサーバーに接続できません: {detail}"
                raise MageVLUnavailableError(self._last_error)

            self._state = "loading"
            self._last_error = ""
            await self._notify(progress_callback, "loading", "動画認識モデルを起動中…（初回はモデルをダウンロードします）")
            try:
                if self._process is None or self._process.poll() is not None:
                    self._start_process()
                timeout = max(1.0, float(self._settings().get("startup_timeout_seconds", 300)))
                await self._wait_ready(timeout)
            except Exception as exc:
                message = str(exc)
                await self.shutdown()
                self._state = "error"
                self._last_error = message
                raise

            self._state = "ready"
            self._last_error = ""
            wait_ms = int((time.monotonic() - started) * 1000)
            self._last_load_wait_ms = wait_ms
            await self._notify(progress_callback, "ready", "動画認識モデルの準備が完了しました")
            return wait_ms

    async def _probe(self) -> bool:
        url = f"{self.base_url().rstrip('/')}/models"
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                response = await client.get(url, headers={"Authorization": f"Bearer {self.api_key()}"})
            if response.is_success:
                self._last_probe_error = ""
                return True
            self._last_probe_error = f"HTTP {response.status_code}"
            return False
        except Exception as exc:
            self._last_probe_error = str(exc)
            return False

    async def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._probe():
                return
            if self._process is not None and self._process.poll() is not None:
                raise MageVLUnavailableError(
                    "Mage-VLサーバーが起動直後に終了しました"
                    f" (exit={self._process.returncode})"
                )
            await asyncio.sleep(1.0)
        detail = self._last_probe_error or "起動タイムアウト"
        raise MageVLUnavailableError(f"Mage-VLサーバーの起動に失敗しました: {detail}")

    def _start_process(self) -> None:
        command = self._server_command()
        root = Path(__file__).resolve().parents[2]
        logger.info("Mage-VLサーバーを起動します: %s", command)
        popen_kwargs: dict[str, Any] = {
            "cwd": str(root),
            "env": {**os.environ, "PYTHONUNBUFFERED": "1"},
            "stdin": subprocess.DEVNULL,
            "stdout": None,
            "stderr": None,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )
        else:
            popen_kwargs["start_new_session"] = True
        self._process = subprocess.Popen(command, **popen_kwargs)

    @staticmethod
    def _signal_process_tree(process: subprocess.Popen[Any], *, force: bool) -> None:
        """Terminate the owned server and all workers it spawned."""
        if os.name == "nt":
            command = ["taskkill", "/PID", str(process.pid), "/T"]
            if force:
                command.append("/F")
            subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        try:
            os.killpg(
                os.getpgid(process.pid),
                signal.SIGKILL if force else signal.SIGTERM,
            )
        except ProcessLookupError:
            pass

    def _server_command(self) -> list[str]:
        settings = self._settings()
        configured = settings.get("server_command") or os.environ.get("AOITALK_MAGE_VL_SERVER_COMMAND")
        if configured:
            if isinstance(configured, list):
                command = [str(item) for item in configured if str(item).strip()]
            else:
                command = shlex.split(str(configured), posix=os.name != "nt")
            if command:
                return command

        parsed = urlsplit(self.base_url())
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 30000
        return [
            sys.executable,
            "-m",
            "sglang.launch_server",
            "--model-path",
            self.model(),
            "--host",
            host,
            "--port",
            str(port),
            "--trust-remote-code",
        ]

    async def shutdown(self) -> None:
        """Stop only a process owned by this adapter."""

        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            if self._state == "ready":
                self._state = "unloaded"
            return
        logger.info("Mage-VLサーバーを停止します: pid=%s", process.pid)
        try:
            try:
                self._signal_process_tree(process, force=False)
            except Exception:
                logger.debug("Mage-VLプロセスグループの停止に失敗しました", exc_info=True)
            if process.poll() is None:
                process.terminate()
            await asyncio.to_thread(process.wait, 10)
        except subprocess.TimeoutExpired:
            try:
                self._signal_process_tree(process, force=True)
            except Exception:
                logger.debug("Mage-VLプロセスグループの強制停止に失敗しました", exc_info=True)
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait, 5)
        except Exception:
            logger.exception("Mage-VLサーバーの停止に失敗しました")
        finally:
            self._state = "unloaded"

    # ------------------------------------------------------------------
    # Official online frame adapter
    # ------------------------------------------------------------------
    async def recognize_video(
        self,
        *,
        path: str | Path,
        question: str,
        num_frames: int = DEFAULT_NUM_FRAMES,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        max_new_tokens: int = DEFAULT_MAX_NEW_TOKENS,
        video_backend: str = "frames",
        progress_callback: ProgressCallback | None = None,
        usage_context: Any = None,
    ) -> MageVLRecognition:
        started = time.monotonic()
        request_gateway = self._request_gateway_snapshot(usage_context)
        request_gateway.ensure_provider_allowed(
            "mage_vl",
            base_url=self.base_url(),
        )
        if video_backend == "codec":
            # The official inference_base.py rejects codec mode for online
            # SGLang inference; keep the same contract here.
            raise ValueError("Mage-VLオンライン動画推論はframesバックエンドのみ対応しています")
        if num_frames <= 0:
            raise ValueError("num_framesは1以上で指定してください")

        load_wait_ms = await self.ensure_ready(progress_callback)
        await self._notify(progress_callback, "analyzing", "動画をMage-VLで解析中…")
        inference_timeout = self.inference_timeout_seconds()
        frame_urls, duration_seconds = await asyncio.wait_for(
            asyncio.to_thread(
                self._sample_video,
                Path(path),
                max(1, int(num_frames)),
                max(0, int(max_pixels)),
            ),
            timeout=inference_timeout,
        )
        if not frame_urls:
            raise ValueError(f"動画からフレームを抽出できませんでした: {path}")

        content: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": frame_url}}
            for frame_url in frame_urls
        ]
        content.append(
            {
                "type": "text",
                "text": question or "この動画を時系列に沿って詳しく説明してください。",
            }
        )
        client = AsyncOpenAI(
            base_url=self.base_url(),
            api_key=self.api_key(),
            timeout=inference_timeout,
        )
        try:
            protected = await request_gateway.protect(
                {"messages": [{"role": "user", "content": content}]},
                provider="mage_vl",
                base_url=self.base_url(),
                source_kind="mage_vl_video",
            )
            protected_messages = (
                protected.payload.get("messages")
                if isinstance(protected.payload, Mapping)
                else None
            )
            if isinstance(protected_messages, list):
                content = protected_messages[0].get("content", content)
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.model(),
                    messages=[{"role": "user", "content": content}],
                    max_tokens=max(1, int(max_new_tokens)),
                ),
                timeout=inference_timeout,
            )
            self._record_usage(
                response,
                started=started,
                usage_context=usage_context,
            )
        finally:
            await client.close()
        result = str(response.choices[0].message.content or "").strip()
        result = str(request_gateway.restore(result) or "").strip()
        await self._notify(progress_callback, "complete", "動画の解析が完了しました")
        return MageVLRecognition(
            result=result,
            load_wait_ms=load_wait_ms,
            num_frames=len(frame_urls),
            duration_seconds=duration_seconds,
        )

    @staticmethod
    def probe_video(path: Path) -> dict[str, float | int | None]:
        """Read lightweight duration/frame metadata without importing cv2."""

        executable = shutil.which("ffprobe")
        if not executable:
            return {"duration_seconds": None, "frame_count": None, "fps": None}
        command = [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,duration,nb_frames,r_frame_rate",
            "-of",
            "json",
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            payload = json.loads(completed.stdout or "{}")
        except Exception:
            return {"duration_seconds": None, "frame_count": None, "fps": None}

        format_data = payload.get("format") or {}
        streams = payload.get("streams") or []
        video_stream = next(
            (
                item
                for item in streams
                if isinstance(item, dict) and item.get("codec_type") == "video"
            ),
            {},
        )

        def _float(value: Any) -> float | None:
            try:
                parsed = float(value)
                return parsed if parsed >= 0 else None
            except (TypeError, ValueError):
                return None

        def _int(value: Any) -> int | None:
            try:
                parsed = int(value)
                return parsed if parsed >= 0 else None
            except (TypeError, ValueError):
                return None

        fps: float | None = None
        rate = str(video_stream.get("r_frame_rate") or "")
        if "/" in rate:
            numerator, denominator = rate.split("/", 1)
            try:
                if float(denominator):
                    fps = float(numerator) / float(denominator)
            except (TypeError, ValueError, ZeroDivisionError):
                fps = None
        duration = _float(format_data.get("duration")) or _float(video_stream.get("duration"))
        frame_count = _int(video_stream.get("nb_frames"))
        if duration is None and frame_count and fps:
            duration = frame_count / fps
        return {
            "duration_seconds": duration,
            "frame_count": frame_count,
            "fps": fps,
        }

    @classmethod
    def _sample_video(
        cls,
        path: Path,
        num_frames: int,
        max_pixels: int,
    ) -> tuple[list[str], float | None]:
        if not path.is_file():
            raise FileNotFoundError(f"動画ファイルが見つかりません: {path}")
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("ffmpegが見つからないためMage-VL用フレームを抽出できません")
        metadata = cls.probe_video(path)
        duration = metadata.get("duration_seconds")
        frame_count = metadata.get("frame_count")
        fps = metadata.get("fps")
        if not isinstance(duration, (int, float)) or duration <= 0:
            if isinstance(frame_count, int) and frame_count > 0 and isinstance(fps, (int, float)) and fps > 0:
                duration = frame_count / fps
            else:
                duration = None

        count = min(num_frames, int(frame_count)) if isinstance(frame_count, int) and frame_count > 0 else num_frames
        if count <= 0:
            count = 1
        if isinstance(duration, (int, float)) and duration > 0:
            frame_period = (1.0 / float(fps)) if isinstance(fps, (int, float)) and fps > 0 else 0.05
            last_timestamp = max(0.0, float(duration) - frame_period)
            timestamps = [
                last_timestamp * index / max(1, count - 1)
                for index in range(count)
            ]
        else:
            timestamps = [0.0]

        urls: list[str] = []
        for timestamp in timestamps:
            command = [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-an",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ]
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                timeout=45,
            )
            if not completed.stdout:
                if urls:
                    continue
                raise ValueError(f"動画の時刻 {timestamp:.3f} 秒からフレームを取得できませんでした")
            with Image.open(io.BytesIO(completed.stdout)) as source:
                frame = source.convert("RGB")
            if max_pixels > 0 and frame.width * frame.height > max_pixels:
                scale = (max_pixels / (frame.width * frame.height)) ** 0.5
                frame = frame.resize(
                    (max(1, int(frame.width * scale)), max(1, int(frame.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=85, optimize=True)
            urls.append(
                "data:image/jpeg;base64,"
                + base64.b64encode(buffer.getvalue()).decode("ascii")
            )
        return urls, float(duration) if isinstance(duration, (int, float)) else None


_instances: dict[int, MageVLService] = {}


def get_mage_vl_service(config: Any, usage_context: Any = None) -> MageVLService:
    """Return the process-shared Mage-VL adapter for the current Config."""

    key = id(config)
    service = _instances.get(key)
    if service is None or service.config is not config:
        service = MageVLService(config, usage_context=usage_context)
        _instances[key] = service
    elif usage_context is not None:
        # The adapter owns a process-shared server, but identity and privacy
        # policy are request-scoped.  Refresh them for every turn instead of
        # retaining the context from the first caller.
        service.set_request_context(usage_context)
    return service


async def shutdown_mage_vl_services() -> None:
    services = list(_instances.values())
    for service in services:
        await service.shutdown()
    _instances.clear()


def clear_mage_vl_services() -> None:
    """Test helper; it intentionally does not stop running processes."""

    _instances.clear()
