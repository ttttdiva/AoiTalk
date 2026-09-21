from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse


MEETING_AUDIO_MAX_DURATION_SECONDS = 8 * 60 * 60


class _CompatibleStageError(RuntimeError):
    """Fallback error used when the worker module is not yet importable."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        retryable: bool,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.retryable = retryable
        self.details = dict(details or {})


def _stage_error(
    code: str,
    message: str,
    *,
    stage: str,
    retryable: bool,
    details: Mapping[str, Any] | None = None,
) -> RuntimeError:
    """Construct the worker's canonical stage error without an import cycle."""
    try:
        # Import only on an error path.  The worker imports this service at
        # composition time, so importing it at module scope would cycle.
        from .meeting_processing_worker import MeetingProcessingStageError
    except Exception:
        return _CompatibleStageError(
            code, message, stage=stage, retryable=retryable, details=details
        )
    return MeetingProcessingStageError(
        code,
        message,
        stage=stage,
        retryable=retryable,
        details=details,
    )


@dataclass(frozen=True)
class MeetingTranscript:
    text: str
    language: str | None
    duration_seconds: float


class MeetingWhisperService:
    """Fail-closed, lazy-loading Whisper adapter for meeting processing.

    Importing or constructing this service is intentionally cheap: neither
    ``whisper`` nor ``torch`` is imported until readiness/transcription is
    requested, and no model is downloaded automatically.
    """

    def __init__(
        self,
        config: Any,
        *,
        model_name: str | None = None,
        cache_dir: Path | None = None,
        device: str | None = None,
        module_loader: Callable[[str], Any] | None = None,
        subprocess_runner: Callable[..., Any] | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self.config = config
        self.model_name = model_name or str(
            self._setting(
                "AOITALK_MEETING_WHISPER_MODEL",
                "meeting_processing.whisper.model",
                "base",
            )
            or "base"
        ).strip()
        if not self.model_name:
            self.model_name = "base"
        raw_cache = cache_dir if cache_dir is not None else self._setting(
            "AOITALK_MEETING_WHISPER_CACHE_DIR",
            "meeting_processing.whisper.cache_dir",
            str(Path.home() / ".cache" / "whisper"),
        )
        self.cache_dir = Path(raw_cache).expanduser()
        self.device = str(
            device
            if device is not None
            else self._setting(
                "AOITALK_MEETING_WHISPER_DEVICE",
                "meeting_processing.whisper.device",
                "auto",
            )
            or "auto"
        ).strip().lower()
        self._module_loader = module_loader or importlib.import_module
        self._subprocess_runner = subprocess_runner or subprocess.run
        self._which = which or shutil.which
        self._model: Any = None
        self._load_lock = asyncio.Lock()
        self._hash_cache: dict[tuple[str, int, int], bool] = {}

    def _setting(self, env_key: str, key: str, default: Any) -> Any:
        env = os.getenv(env_key)
        if env is not None and env.strip() != "":
            return env
        config = self.config
        # Support both nested mappings and config objects exposing a dotted
        # ``get`` method (the two forms are used by AoiTalk deployments).
        try:
            if isinstance(config, Mapping) and key in config:
                value = config.get(key)
                return default if value is None else value
            if config is not None and not isinstance(config, Mapping):
                direct = config.get(key, None)
                if direct is not None:
                    return direct
        except Exception:
            pass
        current = config
        try:
            for part in key.split("."):
                if isinstance(current, Mapping):
                    current = current.get(part)
                else:
                    current = getattr(current, part)
                if current is None:
                    raise KeyError(part)
            return current
        except (AttributeError, KeyError, TypeError):
            # Config objects commonly expose a get("dotted.key") helper.
            try:
                value = config.get(key, default)
                return default if value is None else value
            except Exception:
                return default

    async def start(self) -> None:
        """Lifecycle hook; deliberately does not import or load anything."""
        return None

    async def stop(self) -> None:
        return None

    def _executable_available(self, command: str) -> bool:
        try:
            found = self._which(command)
        except Exception:
            return False
        if not found:
            return False
        # shutil.which returns an executable path.  Test doubles often return
        # a symbolic command name; accept those so probes remain injectable.
        try:
            path = Path(str(found))
            if path.exists():
                return path.is_file() and os.access(str(path), os.X_OK)
        except (OSError, ValueError):
            return False
        return True

    def _load_module(self, name: str) -> Any:
        try:
            return self._module_loader(name)
        except Exception:
            return None

    def _resolve_device(self, torch_module: Any | None = None) -> str | None:
        requested = self.device
        if requested == "auto":
            try:
                available = bool(torch_module.cuda.is_available()) if torch_module else False
            except Exception:
                available = False
            return "cuda" if available else "cpu"
        if requested.startswith("cuda"):
            if torch_module is None:
                return None
            try:
                if not bool(torch_module.cuda.is_available()):
                    return None
                if ":" in requested:
                    index = int(requested.split(":", 1)[1])
                    count_fn = getattr(torch_module.cuda, "device_count", None)
                    if index < 0 or (count_fn is not None and index >= int(count_fn())):
                        return None
            except Exception:
                return None
            return requested
        if requested == "cpu":
            return "cpu"
        return None

    @staticmethod
    def _model_filename(url: Any, model_name: str) -> str:
        try:
            path = unquote(urlparse(str(url)).path)
            name = Path(path).name
        except Exception:
            name = ""
        return name or f"{model_name}.pt"

    @staticmethod
    def _expected_checksum(url: Any) -> str | None:
        match = re.search(r"(?i)([0-9a-f]{64})(?:\b|$)", str(url))
        return match.group(1).lower() if match else None

    def _artifact_info(self, whisper_module: Any) -> tuple[Path, str] | None:
        models = getattr(whisper_module, "_MODELS", None)
        if not isinstance(models, Mapping) or self.model_name not in models:
            return None
        url = models[self.model_name]
        expected = self._expected_checksum(url)
        if not expected:
            return None
        path = self.cache_dir / self._model_filename(url, self.model_name)
        return path, expected

    def _hash_matches(self, path: Path, expected: str) -> bool:
        try:
            stat = path.stat()
            if not path.is_file():
                return False
            key = (str(path), int(stat.st_size), int(stat.st_mtime_ns))
            cached = self._hash_cache.get(key)
            if cached is not None:
                return cached
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result = digest.hexdigest().lower() == expected.lower()
            self._hash_cache[key] = result
            return result
        except (OSError, ValueError):
            return False

    def model_artifact_ready(self, whisper_module: Any | None = None) -> bool:
        module = whisper_module if whisper_module is not None else self._load_module("whisper")
        if module is None:
            return False
        info = self._artifact_info(module)
        if info is None:
            return False
        path, expected = info
        return self._hash_matches(path, expected)

    async def ready(self) -> bool:
        """Probe runtime dependencies without loading a Whisper model."""
        # Imports, PATH lookups and hashing a multi-GB model are blocking I/O.
        # The worker polls this while FastAPI is already serving requests.
        return await asyncio.to_thread(self._ready_sync)

    def _ready_sync(self) -> bool:
        try:
            whisper_module = self._load_module("whisper")
            if whisper_module is None:
                return False
            torch_module = self._load_module("torch")
            if torch_module is None:
                return False
            if not self._executable_available("ffmpeg") or not self._executable_available("ffprobe"):
                return False
            if self._resolve_device(torch_module) is None:
                return False
            return self.model_artifact_ready(whisper_module)
        except Exception:
            return False

    async def readiness(self) -> bool:
        return await self.ready()

    def _run_ffprobe(self, audio_path: Path) -> float:
        if not self._executable_available("ffprobe"):
            raise _stage_error(
                "audio.decode_failed",
                "ffprobe executable is unavailable",
                stage="transcribing",
                retryable=False,
            )
        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ]
        try:
            try:
                completed = self._subprocess_runner(
                    command, capture_output=True, text=True, check=False
                )
            except TypeError:
                completed = self._subprocess_runner(command)
            if inspect.isawaitable(completed):
                completed = asyncio.run(completed)
        except Exception as exc:
            raise _stage_error(
                "audio.decode_failed",
                f"ffprobe failed: {exc}",
                stage="transcribing",
                retryable=False,
            ) from exc
        if isinstance(completed, (tuple, list)):
            returncode = completed[0] if completed else 1
            stdout = completed[1] if len(completed) > 1 else ""
        else:
            returncode = getattr(completed, "returncode", 0)
            stdout = getattr(completed, "stdout", "")
        try:
            if int(returncode or 0) != 0:
                raise ValueError("non-zero ffprobe exit")
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            value = float(str(stdout).strip())
        except (TypeError, ValueError, OverflowError) as exc:
            raise _stage_error(
                "audio.decode_failed",
                "audio duration could not be decoded",
                stage="transcribing",
                retryable=False,
            ) from exc
        if not math.isfinite(value):
            raise _stage_error(
                "audio.decode_failed",
                "audio duration is not finite",
                stage="transcribing",
                retryable=False,
            )
        if value <= 0:
            raise _stage_error(
                "audio.decode_failed",
                "audio duration must be positive",
                stage="transcribing",
                retryable=False,
            )
        if value > MEETING_AUDIO_MAX_DURATION_SECONDS:
            raise _stage_error(
                "audio.duration_exceeded",
                "audio duration exceeds 8 hour limit",
                stage="transcribing",
                retryable=False,
                details={"duration_seconds": value},
            )
        return value

    async def duration(self, audio_path: str | os.PathLike[str]) -> float:
        return await asyncio.to_thread(self._run_ffprobe, Path(audio_path))

    async def transcribe(
        self,
        audio_path: str | os.PathLike[str],
        requested_language: str | None = None,
        *,
        language: str | None = None,
    ) -> MeetingTranscript:
        path = Path(audio_path)
        duration = await self.duration(path)
        async with self._load_lock:
            if self._model is None:
                whisper_module = self._load_module("whisper")
                torch_module = self._load_module("torch")
                resolved_device = self._resolve_device(torch_module)
                if whisper_module is None or torch_module is None or resolved_device is None:
                    raise _stage_error(
                        "whisper.model_unavailable",
                        "Whisper runtime is unavailable",
                        stage="transcribing",
                        retryable=True,
                    )
                # Re-check immediately before loading: whisper.load_model may
                # otherwise download a missing artifact or silently substitute.
                if not self.model_artifact_ready(whisper_module):
                    raise _stage_error(
                        "whisper.model_unavailable",
                        f"configured Whisper model {self.model_name!r} is unavailable",
                        stage="transcribing",
                        retryable=True,
                    )
                try:
                    self._model = whisper_module.load_model(
                        self.model_name,
                        device=resolved_device,
                        download_root=str(self.cache_dir),
                    )
                except Exception as exc:
                    raise _stage_error(
                        "whisper.model_unavailable",
                        f"failed to load configured Whisper model {self.model_name!r}",
                        stage="transcribing",
                        retryable=True,
                    ) from exc
            model = self._model
            resolved_device = self._resolve_device(self._load_module("torch")) or "cpu"
        lang = requested_language if requested_language is not None else language
        try:
            result = await asyncio.to_thread(
                model.transcribe,
                str(path),
                language=lang,
                verbose=False,
                fp16=(resolved_device == "cuda"),
            )
        except Exception as exc:
            raise _stage_error(
                "whisper.transcription_failed",
                "Whisper transcription failed",
                stage="transcribing",
                retryable=True,
            ) from exc
        if isinstance(result, Mapping):
            text = str(result.get("text") or "")
            out_language = result.get("language")
            out_language = str(out_language) if out_language is not None else lang
        else:
            text = str(getattr(result, "text", "") or "")
            out_language = getattr(result, "language", None) or lang
        return MeetingTranscript(text=text, language=out_language, duration_seconds=duration)

    async def process(
        self,
        audio_path: str | os.PathLike[str],
        requested_language: str | None = None,
        *,
        language: str | None = None,
    ) -> MeetingTranscript:
        """Compatibility alias used by generic processing adapters."""
        return await self.transcribe(
            audio_path, requested_language=requested_language, language=language
        )


__all__ = ["MeetingTranscript", "MeetingWhisperService", "MEETING_AUDIO_MAX_DURATION_SECONDS"]
