"""Windows WASAPI render-loopback recorder.

The optional :mod:`pyaudiowpatch` import is deliberately delayed until a
device operation is requested.  This keeps Linux/CI imports and the rest of
the audio stack usable when the Windows-only dependency is absent.

PyAudio callbacks only enqueue bytes.  A bounded queue and a dedicated worker
perform all file I/O, so a slow filesystem cannot block the audio callback.
"""

from __future__ import annotations

import importlib
import hashlib
import logging
import os
import platform
import queue
import threading
import time
import uuid
import wave
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


MAX_CAPTURE_SECONDS = 120.0
DEFAULT_QUEUE_SIZE = 64
DEFAULT_FRAMES_PER_BUFFER = 1024


class WasapiLoopbackError(RuntimeError):
    """WASAPI loopback が利用できない/失敗した。"""

    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class CaptureAlreadyRunningError(WasapiLoopbackError):
    def __init__(self):
        super().__init__("同時に録音できる PC スピーカー録音は1件だけです", status_code=409)


class CaptureNotFoundError(WasapiLoopbackError):
    def __init__(self, capture_id: str):
        super().__init__(f"録音が見つかりません: {capture_id}", status_code=404)


@dataclass
class _CaptureState:
    capture_id: str
    device: dict[str, Any]
    output_path: Path
    temp_path: Path
    sample_rate: int
    channels: int
    queue: queue.Queue[Any]
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    status: str = "recording"
    error: Optional[str] = None
    dropped_chunks: int = 0
    frames_written: int = 0
    stream: Any = None
    pyaudio_instance: Any = None
    worker: Optional[threading.Thread] = None
    timer: Optional[threading.Timer] = None
    stop_requested: bool = False
    worker_done: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)


class WasapiLoopbackRecorder:
    """単一の WASAPI render-loopback capture を管理する。"""

    def __init__(
        self,
        output_root: str | os.PathLike[str] | None = None,
        *,
        max_seconds: float = MAX_CAPTURE_SECONDS,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        frames_per_buffer: int = DEFAULT_FRAMES_PER_BUFFER,
        pyaudio_module: Any = None,
        platform_name: str | None = None,
        on_complete: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        if output_root is None:
            configured_data = os.environ.get("AOITALK_DATA_DIR")
            if configured_data and str(configured_data).strip():
                data_root = Path(str(configured_data)).expanduser()
                if not data_root.is_absolute():
                    data_root = repo_root / data_root
                root = data_root / "character_voice_captures"
            else:
                root = repo_root / "data" / "character_voice_captures"
        else:
            root = Path(output_root).expanduser()
            if not root.is_absolute():
                root = repo_root / root
        self.output_root = root.resolve()
        self.max_seconds = max(1.0, min(float(max_seconds), MAX_CAPTURE_SECONDS))
        self.queue_size = max(2, int(queue_size))
        self.frames_per_buffer = max(64, int(frames_per_buffer))
        self._pyaudio_module = pyaudio_module
        self._platform_name = platform_name or platform.system()
        self._on_complete = on_complete
        self._lock = threading.RLock()
        self._active: Optional[_CaptureState] = None
        self._captures: dict[str, _CaptureState] = {}
        self._closed = False

    # ------------------------------------------------------------------
    # Optional dependency / WASAPI devices
    # ------------------------------------------------------------------
    def _load_pyaudio(self) -> Any:
        if self._platform_name.lower() != "windows":
            raise WasapiLoopbackError("PCスピーカー録音はWindows WASAPI環境でのみ利用できます")
        if self._pyaudio_module is None:
            try:
                self._pyaudio_module = importlib.import_module("pyaudiowpatch")
            except Exception as exc:
                raise WasapiLoopbackError(
                    "pyaudiowpatch がインストールされていないためPCスピーカー録音を利用できません"
                ) from exc
        return self._pyaudio_module

    def _pyaudio_instance(self, module: Any) -> Any:
        try:
            return module.PyAudio()
        except Exception as exc:
            raise WasapiLoopbackError(f"WASAPIオーディオを初期化できません: {exc}") from exc

    def _loopback_devices(self, audio: Any) -> list[dict[str, Any]]:
        """pyaudiowpatch が提供する render-loopback device のみ返す。"""

        candidates: list[dict[str, Any]] = []
        generator = getattr(audio, "get_loopback_device_info_generator", None)
        if callable(generator):
            try:
                values = generator()
                candidates.extend(dict(value) for value in values)
            except Exception as exc:
                logger.debug("loopback device generator failed: %s", exc)
        if not candidates:
            # Some test doubles/older pyaudiowpatch builds only expose
            # get_device_count/get_device_info_by_index.  Filter the list by
            # loopback marker where available, never by microphone semantics.
            count = int(getattr(audio, "get_device_count", lambda: 0)())
            for index in range(max(0, count)):
                try:
                    info = dict(audio.get_device_info_by_index(index))
                except Exception:
                    continue
                if info.get("isLoopbackDevice") or "loopback" in str(info.get("name", "")).lower():
                    candidates.append(info)

        default_index: Optional[int] = None
        default_loopback: Optional[dict[str, Any]] = None
        # PyAudioWPatch provides a direct default-loopback lookup.  Prefer it
        # over matching render-device names, which is unreliable with localized
        # names and Bluetooth endpoints.
        default_getter = getattr(audio, "get_default_wasapi_loopback", None)
        if callable(default_getter):
            try:
                raw_default = default_getter()
                if isinstance(raw_default, Mapping):
                    default_loopback = dict(raw_default)
                    default_index = int(
                        default_loopback.get(
                            "index",
                            default_loopback.get("deviceIndex", -1),
                        )
                    )
                    if default_index < 0:
                        default_index = None
                elif raw_default is not None:
                    # A few wrappers expose only the numeric index rather than
                    # the full PyAudio device-info mapping.
                    default_index = int(raw_default)
                    if default_index < 0:
                        default_index = None
            except Exception as exc:
                logger.debug("get_default_wasapi_loopback failed: %s", exc)
        if default_loopback is not None and default_index is not None:
            known_indexes = {int(item.get("index", -1)) for item in candidates}
            if default_index not in known_indexes:
                # A test double or an older build may omit the default from its
                # generator; include the direct result so it remains selectable.
                default_loopback.setdefault("isLoopbackDevice", True)
                default_loopback.setdefault("index", default_index)
                candidates.append(default_loopback)
        if default_index is None:
            # Compatibility fallback for older PyAudioWPatch releases.  Name
            # matching is intentionally only a fallback, never the primary
            # default-device selection path.
            try:
                wasapi = audio.get_host_api_info_by_type(
                    getattr(self._pyaudio_module, "paWASAPI", 13)
                )
                default_info = audio.get_device_info_by_index(
                    int(wasapi.get("defaultOutputDevice", -1))
                )
                default_name = str(default_info.get("name", ""))
                for item in candidates:
                    if item.get("isDefault") or str(item.get("name", "")) == default_name:
                        default_index = int(item.get("index"))
                        break
                if default_index is None:
                    default_index = int(default_info.get("index", -1))
            except Exception:
                pass

        result: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for item in candidates:
            try:
                index = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            channels = int(item.get("maxInputChannels") or item.get("maxOutputChannels") or 2)
            sample_rate = int(float(item.get("defaultSampleRate") or 48_000))
            if channels < 1:
                continue
            raw_id = str(item.get("id") or item.get("device_id") or "").strip()
            if not raw_id:
                # PyAudio's numeric index can change when devices are plugged
                # or unplugged.  Prefer a deterministic identity derived from
                # the WASAPI host/name pair so the client can safely persist
                # it between device-list refreshes.
                host_api = str(item.get("hostApi") or item.get("host_api") or "wasapi")
                name = str(item.get("name") or f"Loopback device {index}").strip()
                digest = hashlib.sha256(f"{host_api}\x00{name}".encode("utf-8", "replace")).hexdigest()[:20]
                raw_id = f"wasapi-{digest}"
            if raw_id in seen_ids:
                # Duplicate endpoint names are uncommon but valid (for
                # example two identical USB outputs).  Keep IDs unique while
                # retaining the deterministic base identity.
                raw_id = f"{raw_id}-{index}"
            seen_ids.add(raw_id)
            result.append(
                {
                    "id": raw_id,
                    "index": index,
                    "name": str(item.get("name") or f"Loopback device {index}"),
                    "channels": channels,
                    "sample_rate": sample_rate,
                    "is_default": index == default_index,
                    "is_loopback": True,
                }
            )
        result.sort(key=lambda value: (not bool(value["is_default"]), value["name"].casefold(), value["index"]))
        return result

    def list_devices(self) -> list[dict[str, Any]]:
        module = self._load_pyaudio()
        audio = self._pyaudio_instance(module)
        try:
            return self._loopback_devices(audio)
        finally:
            try:
                audio.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Capture lifecycle
    # ------------------------------------------------------------------
    def start(
        self,
        device_index: int | None = None,
        *,
        device_id: str | None = None,
        character_id: str | None = None,
    ) -> dict[str, Any]:
        module = self._load_pyaudio()
        with self._lock:
            if self._closed:
                raise WasapiLoopbackError("録音サービスは終了しています", status_code=503)
            if self._active is not None and self._active.status in {"recording", "stopping"}:
                raise CaptureAlreadyRunningError()

            audio = self._pyaudio_instance(module)
            try:
                devices = self._loopback_devices(audio)
                if not devices:
                    raise WasapiLoopbackError("WASAPI出力ループバックデバイスが見つかりません")
                selected = None
                if device_id is not None and str(device_id).strip():
                    requested_id = str(device_id).strip()
                    selected = next((item for item in devices if item["id"] == requested_id), None)
                    if selected is None:
                        raise WasapiLoopbackError(
                            "指定されたループバックデバイスが見つかりません（再読み込みしてください）",
                            404,
                        )
                elif device_index is not None:
                    selected = next((item for item in devices if item["index"] == int(device_index)), None)
                    if selected is None:
                        raise WasapiLoopbackError("指定されたループバックデバイスが見つかりません", 404)
                else:
                    selected = next((item for item in devices if item.get("is_default")), devices[0])

                capture_id = str(uuid.uuid4())
                self.output_root.mkdir(parents=True, exist_ok=True)
                output_path = (self.output_root / f"{capture_id}.wav").resolve()
                root = self.output_root.resolve()
                output_path.relative_to(root)
                temp_path = output_path.with_suffix(".wav.part")
                sample_rate = int(selected["sample_rate"])
                channels = max(1, min(int(selected["channels"]), 32))
                state = _CaptureState(
                    capture_id=capture_id,
                    device={**selected, "character_id": character_id},
                    output_path=output_path,
                    temp_path=temp_path,
                    sample_rate=sample_rate,
                    channels=channels,
                    queue=queue.Queue(maxsize=self.queue_size),
                    pyaudio_instance=audio,
                )
                # The worker owns this file.  The callback never touches disk.
                state.worker = threading.Thread(
                    target=self._worker_main,
                    args=(state,),
                    name=f"wasapi-loopback-{capture_id[:8]}",
                    daemon=True,
                )
                state.worker.start()

                pa_format = getattr(module, "paInt16", 8)

                def callback(in_data: bytes, frame_count: int, time_info: Any, status: Any):
                    del time_info, status
                    with state.lock:
                        if state.stop_requested or state.status not in {"recording", "stopping"}:
                            return (None, getattr(module, "paComplete", 1))
                        try:
                            state.queue.put_nowait((bytes(in_data or b""), int(frame_count or 0)))
                        except queue.Full:
                            # Keep callback bounded and non-blocking.  Missing
                            # chunks are surfaced in status instead of blocking
                            # or performing disk I/O in the audio callback.
                            state.dropped_chunks += 1
                    return (None, getattr(module, "paContinue", 0))

                try:
                    stream = audio.open(
                        format=pa_format,
                        channels=channels,
                        rate=sample_rate,
                        input=True,
                        input_device_index=int(selected["index"]),
                        frames_per_buffer=self.frames_per_buffer,
                        stream_callback=callback,
                    )
                    state.stream = stream
                    if hasattr(stream, "start_stream"):
                        stream.start_stream()
                except Exception as exc:
                    state.stop_requested = True
                    try:
                        state.queue.put(None, timeout=2.0)
                    except queue.Full:
                        state.error = "録音workerを停止できませんでした"
                    state.worker_done.wait(timeout=2)
                    try:
                        audio.terminate()
                    except Exception:
                        pass
                    raise WasapiLoopbackError(f"WASAPI録音を開始できません: {exc}") from exc

                state.timer = threading.Timer(self.max_seconds, self._auto_stop, args=(capture_id,))
                state.timer.daemon = True
                state.timer.start()
                self._active = state
                self._captures[capture_id] = state
                return self._status_dict(state)
            except Exception:
                try:
                    audio.terminate()
                except Exception:
                    pass
                raise

    def _worker_main(self, state: _CaptureState) -> None:
        try:
            with wave.open(str(state.temp_path), "wb") as output:
                output.setnchannels(state.channels)
                output.setsampwidth(2)
                output.setframerate(state.sample_rate)
                while True:
                    item = state.queue.get()
                    if item is None:
                        break
                    payload, frame_count = item
                    if payload:
                        output.writeframes(payload)
                        state.frames_written += max(0, int(frame_count))
                output.close()
            # A completed file is atomically exposed only after the worker has
            # closed the RIFF header and flushed all queued frames.
            os.replace(state.temp_path, state.output_path)
        except Exception as exc:
            state.error = str(exc)
            try:
                state.temp_path.unlink(missing_ok=True)
            except Exception:
                pass
        finally:
            with state.lock:
                if state.status not in {"failed", "cancelled"}:
                    state.status = "ready" if state.error is None else "failed"
                state.ended_at = state.ended_at or time.time()
            # Publish terminal status before waking stop()/shutdown waiters.
            state.worker_done.set()
            if state.error is None and self._on_complete is not None:
                try:
                    self._on_complete(self._status_dict(state))
                except Exception:
                    logger.exception("WASAPI録音完了コールバックに失敗しました")

    def _auto_stop(self, capture_id: str) -> None:
        try:
            # The timer thread is not the audio callback; waiting here lets us
            # close the RIFF header before another capture can start.
            self.stop(capture_id, wait=True, reason="max_duration")
        except Exception:
            logger.debug("WASAPI自動停止に失敗", exc_info=True)

    @staticmethod
    def _signal_worker_stop(state: _CaptureState, *, timeout: float = 10.0) -> bool:
        """worker queue に sentinel を投入し、既存chunkを捨てずに drain させる。"""

        if state.worker_done.is_set():
            return True
        # This runs on the request/timer/shutdown thread, never inside the
        # PortAudio callback.  Blocking here is intentional: the worker must
        # drain queued audio before seeing the sentinel.  Retry in short
        # intervals so an already-failed worker wakes stop() promptly instead
        # of leaving the caller blocked for the whole timeout.
        deadline = time.monotonic() + max(0.1, float(timeout))
        while not state.worker_done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                # Let the worker consume queued chunks, then enqueue sentinel;
                # never discard the queue just to make room for shutdown.
                state.queue.put(None, timeout=min(0.25, remaining))
                break
            except queue.Full:
                continue
        if state.worker_done.is_set():
            return True
        remaining = deadline - time.monotonic()
        return remaining > 0 and state.worker_done.wait(timeout=remaining)

    def stop(self, capture_id: str, *, wait: bool = True, reason: str = "user") -> dict[str, Any]:
        with self._lock:
            state = self._captures.get(str(capture_id))
            if state is None:
                raise CaptureNotFoundError(str(capture_id))
            with state.lock:
                if state.status in {"ready", "failed", "cancelled"}:
                    return self._status_dict(state)
                state.stop_requested = True
                state.status = "stopping"
                state.ended_at = state.ended_at or time.time()
            if state.timer is not None and state.timer is not threading.current_thread():
                state.timer.cancel()
            stream = state.stream
            try:
                if stream is not None and hasattr(stream, "stop_stream"):
                    stream.stop_stream()
            except Exception as exc:
                logger.debug("WASAPI stream stop failed: %s", exc)
            try:
                if stream is not None and hasattr(stream, "close"):
                    stream.close()
            except Exception as exc:
                logger.debug("WASAPI stream close failed: %s", exc)

        # Never hold the recorder registry lock while waiting for the worker:
        # completion callbacks/status readers may need the same registry.
        worker_finished = self._signal_worker_stop(state, timeout=10.0 if wait else 2.0)

        if wait:
            worker_finished = state.worker_done.wait(timeout=10) or worker_finished
        else:
            worker_finished = state.worker_done.is_set() or worker_finished
        if not worker_finished and not state.worker_done.is_set():
            with state.lock:
                state.error = state.error or "録音workerが停止しませんでした"
                state.status = "failed"
        try:
            if state.pyaudio_instance is not None:
                state.pyaudio_instance.terminate()
        except Exception as exc:
            logger.debug("WASAPI terminate failed: %s", exc)
        with self._lock:
            if self._active is state:
                self._active = None
        return self._status_dict(state)

    def get_status(self, capture_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._captures.get(str(capture_id))
            if state is None:
                raise CaptureNotFoundError(str(capture_id))
            return self._status_dict(state)

    def take_output(self, capture_id: str) -> Path:
        """完成済み capture のパスを取得する（削除は呼び出し側が行う）。"""

        with self._lock:
            state = self._captures.get(str(capture_id))
            if state is None:
                raise CaptureNotFoundError(str(capture_id))
            if state.status != "ready" or not state.output_path.is_file():
                if state.error:
                    raise WasapiLoopbackError(f"録音ファイルの生成に失敗しました: {state.error}", 500)
                raise WasapiLoopbackError("録音がまだ完了していません", 409)
            return state.output_path

    def cleanup_capture(self, capture_id: str) -> None:
        with self._lock:
            state = self._captures.pop(str(capture_id), None)
            if state is None:
                return
            for path in (state.output_path, state.temp_path):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass

    def _status_dict(self, state: _CaptureState) -> dict[str, Any]:
        with state.lock:
            ended = state.ended_at
            elapsed = (ended or time.time()) - state.started_at
            return {
                "capture_id": state.capture_id,
                "status": state.status,
                "started_at": state.started_at,
                "ended_at": ended,
                "duration_seconds": round(max(0.0, min(elapsed, self.max_seconds)), 6),
                "device": dict(state.device),
                "sample_rate": state.sample_rate,
                "channels": state.channels,
                "frames_written": state.frames_written,
                "dropped_chunks": state.dropped_chunks,
                "error": state.error,
                "output_ready": state.output_path.is_file() and state.status == "ready",
            }

    def shutdown(self) -> None:
        with self._lock:
            captures = list(self._captures)
            self._closed = True
        for capture_id in captures:
            try:
                self.stop(capture_id)
            except Exception:
                logger.debug("WASAPI capture shutdown cleanup failed", exc_info=True)
            self.cleanup_capture(capture_id)

    close = shutdown

    def __enter__(self) -> "WasapiLoopbackRecorder":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown()


__all__ = [
    "CaptureAlreadyRunningError",
    "CaptureNotFoundError",
    "WasapiLoopbackError",
    "WasapiLoopbackRecorder",
]
