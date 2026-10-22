"""One shared host wrapper around the private EmbeddingGemma 2 worker."""

from __future__ import annotations
import asyncio
import atexit
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
import logging
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import tempfile
import time
from typing import Any
import uuid

from .config import EmbeddingConfig
from .embedding_identity import (
    DIMENSION,
    EMBEDDING_FINGERPRINT,
    EMBEDDING_IDENTITY,
    document_input,
)
from .embedding_text import safe_embedding_request_diagnostics
from .index_io import run_index_io

logger = logging.getLogger(__name__)

_SAFE_WORKER_REQUEST_ERROR_CODES = frozenset(
    {"textual_multimodal_placeholder_without_image", "embedding_input_rejected"}
)


class EmbeddingRequestRejectedError(RuntimeError):
    """A worker rejected one request while its private process stayed healthy."""

    def __init__(self, error_code: str, diagnostics: dict[str, Any]):
        self.error_code = (
            error_code
            if error_code in _SAFE_WORKER_REQUEST_ERROR_CODES
            else "embedding_input_rejected"
        )
        self.diagnostics = safe_embedding_request_diagnostics(diagnostics)
        super().__init__(self.error_code)


class EmbeddingGemma2Embedding:
    """Persistent private IPC; no host torch/ST import or alternative encoder.

    One serialized request is active at a time. Timeout, cancellation, broken
    pipes and protocol errors stop that process; a subsequent request starts
    and loads a fresh process. Failures remain observable, without old vectors.
    """

    identity = EMBEDDING_IDENTITY
    fingerprint = EMBEDDING_FINGERPRINT

    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self.model = None
        self._initialized = False
        self._closed = False
        self._admission_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._pending_cancellations = set()
        self._active_operation = None
        self._releasing = False
        self._idle_timer = None
        self._idle_generation = 0
        self._last_used_at = None
        self._last_release_reason = None
        self._idle_timeout_seconds = float(getattr(config, "idle_timeout_seconds", 300.0))
        if not math.isfinite(self._idle_timeout_seconds) or self._idle_timeout_seconds < 0:
            raise ValueError("idle_timeout_seconds must be finite and non-negative")
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="embeddinggemma2-ipc"
        )
        self._process = None
        self._responses = None
        self._log = None
        self.configured_device = config.device
        self.effective_device = None
        self.precision = None
        self.last_error = None
        self.metrics = {}
        self._temporary = None
        atexit.register(self.close)

    def _settings(self):
        return {
            "device": self.config.device,
            "cache_dir": self.config.cache_dir,
            "local_files_only": self.config.local_files_only,
        }

    def _start(self):
        if self._closed:
            raise RuntimeError("embedding_runtime_closed")
        if self._process is not None and self._process.poll() is None:
            return
        self._stop()
        python = (
            Path(self.config.worker_python)
            if self.config.worker_python
            else Path("cache/embeddinggemma2/runtime/venv")
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        if not python.is_file():
            raise RuntimeError(
                "Embedding worker environment missing; run scripts/setup_embeddinggemma2_worker.py"
            )
        logs = Path("logs")
        logs.mkdir(parents=True, exist_ok=True)
        self._log = (logs / "embeddinggemma2-worker.log").open("ab")
        # Do not inherit a host PYTHONPATH/PYTHONHOME that bypasses isolation.
        env = {
            k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}
        }
        env["PYTHONIOENCODING"] = "utf-8"
        scratch = Path(__file__).resolve().parents[2] / ".local"
        scratch.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix="eg2-worker-", dir=scratch)
        env["AOITALK_EMBEDDING_TMP_DIR"] = self._temporary.name
        script = Path(__file__).with_name("embedding_worker.py")
        process = subprocess.Popen(
            [str(python.resolve()), "-u", str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            env=env,
        )
        responses = queue.Queue()
        self._responses = responses
        self._process = process

        def reader():
            try:
                while line := process.stdout.readline():
                    responses.put(json.loads(line))
            except Exception as exc:
                responses.put(exc)
            finally:
                responses.put(RuntimeError("embedding_worker_exited"))

        threading.Thread(
            target=reader, name="embeddinggemma2-reader", daemon=True
        ).start()

    def _stop(self):
        process = self._process
        self._responses = None
        self._initialized = False
        self.effective_device = None
        self.precision = None
        self.metrics = {}
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self._process = None
            for handle in (process.stdin, process.stdout):
                if handle:
                    handle.close()
        if self._log:
            self._log.close()
            self._log = None
        if self._temporary:
            self._temporary.cleanup()
            self._temporary = None

    def _exchange(self, op, payload, request_id, cancelled):
        if cancelled.is_set():
            raise RuntimeError("embedding_request_cancelled")
        self._start()
        request = {
            "request_id": request_id,
            "identity": self.fingerprint,
            "op": op,
            **payload,
        }
        encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode()
        if len(encoded) > 128 * 1024 * 1024:
            raise ValueError("embedding_request_too_large")
        deadline = time.monotonic() + self.config.request_timeout_seconds
        writes = queue.Queue()
        process = self._process

        def write_request():
            try:
                process.stdin.write(encoded)
                process.stdin.flush()
                writes.put(None)
            except Exception as exc:
                writes.put(exc)

        threading.Thread(
            target=write_request, name="embeddinggemma2-writer", daemon=True
        ).start()
        while True:
            if cancelled.is_set():
                self._stop()
                raise RuntimeError("embedding_request_cancelled")
            if time.monotonic() > deadline:
                self._stop()
                raise TimeoutError("embedding_worker_write_timeout")
            try:
                write_error = writes.get(timeout=0.1)
            except queue.Empty:
                continue
            if write_error is not None:
                self._stop()
                raise write_error
            break
        while True:
            if cancelled.is_set():
                self._stop()
                raise RuntimeError("embedding_request_cancelled")
            if time.monotonic() > deadline:
                self._stop()
                raise TimeoutError("embedding_worker_request_timeout")
            try:
                response = self._responses.get(
                    timeout=min(0.1, max(0.001, deadline - time.monotonic()))
                )
            except queue.Empty:
                continue
            if isinstance(response, Exception):
                self._stop()
                raise response
            if (
                response.get("request_id") != request_id
                or response.get("identity") != self.fingerprint
            ):
                self._stop()
                raise RuntimeError("embedding_worker_protocol_identity_mismatch")
            if not response.get("ok"):
                if response.get("error_category") == "request":
                    raise EmbeddingRequestRejectedError(
                        response.get("error_code", "embedding_input_rejected"),
                        response.get("diagnostics"),
                    )
                raise RuntimeError("embedding_worker_runtime_error")
            self.metrics = response.get("metrics", {})
            return response

    def _request_sync(self, op, payload, request_id, cancelled):
        # Serialize terminal close with all worker creation/exchange/teardown.
        with self._lifecycle_lock:
            with self._admission_lock:
                if self._closed or cancelled.is_set():
                    return None
                self._active_operation = op
            try:
                return self._request_sync_owned(op, payload, request_id, cancelled)
            finally:
                with self._admission_lock:
                    self._active_operation = None
                    self._last_used_at = time.monotonic()

    def _accept_initialization(self, response):
        if response.get("dimension") != DIMENSION:
            raise RuntimeError("embedding_dimension_mismatch")
        self.effective_device = response["device"]
        self.precision = response["precision"]
        self._initialized = True

    def _request_sync_owned(self, op, payload, request_id, cancelled):
        try:
            if (op == "initialize" and self._initialized
                    and self._process is not None and self._process.poll() is None):
                return {"ok": True, "dimension": DIMENSION,
                        "device": self.effective_device, "precision": self.precision}
            if op != "initialize" and (
                not self._initialized
                or self._process is None
                or self._process.poll() is not None
            ):
                self._accept_initialization(self._exchange(
                    "initialize",
                    {"settings": self._settings()},
                    uuid.uuid4().hex,
                    cancelled,
                ))
            response = self._exchange(op, payload, request_id, cancelled)
            if op == "initialize":
                self._accept_initialization(response)
            self.last_error = None
            return response
        except EmbeddingRequestRejectedError as exc:
            self.last_error = exc.error_code
            diagnostics = exc.diagnostics
            logger.warning(
                "EmbeddingGemma 2 request rejected: code=%s mode=%s input_types=%s "
                "image_count=%d special_token_counts=%s",
                exc.error_code,
                diagnostics["mode"],
                diagnostics["input_types"],
                diagnostics["image_count"],
                diagnostics["special_token_counts"],
            )
            # The worker returned a request-scoped rejection and remains alive.
            # Do not unload/reload the model for a malformed or unrepresentable
            # text input; a later independent request can still use this process.
            return None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop()
            logger.error("EmbeddingGemma 2 worker unavailable: %s", self.last_error)
            return None

    async def _request(self, op, payload):
        cancelled = threading.Event()
        with self._admission_lock:
            if self._closed:
                raise RuntimeError("embedding_runtime_closed")
            self._cancel_idle_timer_locked()
            self._pending_cancellations.add(cancelled)
            try:
                future = asyncio.get_running_loop().run_in_executor(
                    self._executor, self._request_sync, op, payload, uuid.uuid4().hex, cancelled
                )
            except BaseException:
                self._pending_cancellations.discard(cancelled)
                self._schedule_idle_timer_locked()
                raise
        try:
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled.set()
                # The IPC worker drains/stops before another request can run.
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                future.result()
                raise
        finally:
            with self._admission_lock:
                self._pending_cancellations.discard(cancelled)
                self._schedule_idle_timer_locked()

    async def initialize(self):
        if self._closed:
            return False
        # Even the warm fast path is admitted and serialized with release.
        return bool(await self._request("initialize", {"settings": self._settings()}))

    async def _encode(self, inputs: list[Any], *, mode: str):
        if not inputs:
            return []
        vectors = []
        for offset in range(0, len(inputs), min(32, max(1, self.config.batch_size))):
            batch = inputs[offset : offset + min(32, max(1, self.config.batch_size))]
            response = await self._request(
                "encode",
                {"inputs": batch, "mode": mode, "batch_size": self.config.batch_size},
            )
            result = response.get("vectors", []) if response else []
            if len(result) != len(batch) or any(
                len(v) != DIMENSION
                or not all(math.isfinite(float(x)) for x in v)
                or abs(sum(float(x) ** 2 for x in v) - 1) > 0.01
                for v in result
            ):
                self.last_error = (
                    self.last_error
                    or "Invalid embedding count, dimension, finite values or norm"
                )
                return []
            vectors.extend(result)
        return vectors

    async def embed(self, texts: list[str], *, titles: list[str] | None = None):
        if titles is not None and len(titles) != len(texts):
            raise ValueError("Each document needs its title")
        return await self._encode(
            [
                document_input(text, titles[i] if titles else None)
                for i, text in enumerate(texts)
            ],
            mode="document",
        )

    async def embed_query(self, query: str, *, code: bool = False):
        result = await self._encode(
            [query], mode="CodeRetrieval" if code else "SearchQuery"
        )
        return result[0] if result else None

    @staticmethod
    def _pack_media(item):
        import numpy as np
        from PIL import Image

        packed = {}
        for modality, value in item.items():
            if modality == "image":
                if not isinstance(value, Image.Image):
                    with Image.open(value) as image:
                        value = image.convert("RGB").copy()
                buffer = io.BytesIO()
                value.save(buffer, format="PNG")
                raw = buffer.getvalue()
            elif modality == "audio":
                values = np.asarray(value, dtype="<f4")
                if (
                    values.ndim != 1
                    or not 0 < len(values) <= 480000
                    or not np.isfinite(values).all()
                ):
                    raise ValueError("Invalid mono16k audio chunk")
                raw = values.tobytes()
            elif modality == "video":
                # Host callers obtain this derived clip from MediaPreparationService
                # only after resolving the original's current ACL.
                raw = Path(value).read_bytes()
            else:
                raise ValueError("Unsupported native media key")
            if len(raw) > 64 * 1024 * 1024:
                raise ValueError("Media chunk exceeds IPC limit")
            packed[modality] = {
                "encoding": "base64",
                "data": base64.b64encode(raw).decode("ascii"),
            }
        return packed

    async def encode_media(self, inputs):
        vectors = []
        size = min(32, max(1, self.config.batch_size))
        for offset in range(0, len(inputs), size):
            packed = await run_index_io(
                lambda: [
                    self._pack_media(item) for item in inputs[offset : offset + size]
                ]
            )
            result = await self._encode(packed, mode="media")
            if len(result) != len(packed):
                return []
            vectors.extend(result)
        return vectors

    @classmethod
    def _pack_query(cls, item):
        """Query-only text/list extension; media IPC still carries bytes only."""
        if isinstance(item, str):
            return item
        if not isinstance(item, dict) or not item or set(item) - {"text", "image", "audio", "video"}:
            raise ValueError("invalid_native_query")
        if set(item) == {"text"}:
            if not isinstance(item["text"], str):
                raise ValueError("invalid_query_text")
            return item["text"]
        packed = {}
        for modality, value in item.items():
            if modality == "text":
                if not isinstance(value, str):
                    raise ValueError("invalid_query_text")
                packed[modality] = value
                continue
            values = value if isinstance(value, list) else [value]
            if not values or len(values) > 16:
                raise ValueError("invalid_query_media_list")
            if modality == "image":
                from PIL import Image
                if any(not isinstance(v, Image.Image) for v in values):
                    raise ValueError("query_image_requires_decoded_original")
            elif modality == "audio":
                if any(isinstance(v, (str, Path)) for v in values):
                    raise ValueError("query_audio_requires_decoded_original")
            elif modality == "video":
                from .media_query import require_query_video
                values = [require_query_video(v) for v in values]
            # Video is a server-created clip from MediaQueryPreparer. The
            # worker receives its bytes, never a client path or URL.
            packed[modality] = [cls._pack_media({modality: v})[modality] for v in values]
        return packed

    async def encode_query_inputs(self, inputs, *, max_tokens=8192):
        if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
            raise ValueError("invalid_query_token_budget")
        size = min(32, max(1, self.config.batch_size))
        from .index_io import run_index_io
        batches = [inputs[i:i + size] for i in range(0, len(inputs), size)]
        # Check every window before any inference. No prefix becomes a
        # successful result when a distant PDF page exceeds the budget.
        for batch in batches:
            packed_holder = [None]
            try:
                await run_index_io(
                    lambda: packed_holder.__setitem__(
                        0, [self._pack_query(item) for item in batch]
                    )
                )
                packed = packed_holder.pop()
            except BaseException:
                packed_holder.clear()
                raise
            try:
                if not await self._request(
                    "validate_query", {"inputs": packed, "max_tokens": max_tokens}
                ):
                    return []
            finally:
                packed_holder.clear()
                del packed
        vectors = []
        for batch in batches:
            packed_holder = [None]
            try:
                await run_index_io(
                    lambda: packed_holder.__setitem__(
                        0, [self._pack_query(item) for item in batch]
                    )
                )
                packed = packed_holder.pop()
            except BaseException:
                packed_holder.clear()
                raise
            try:
                response = await self._request(
                    "encode",
                    {
                        "inputs": packed,
                        "mode": "query",
                        "max_tokens": max_tokens,
                        "batch_size": size,
                    },
                )
            finally:
                packed_holder.clear()
                del packed
            result = response.get("vectors", []) if response else []
            if len(result) != len(batch) or any(len(v) != DIMENSION
                    or not all(math.isfinite(float(x)) for x in v)
                    or abs(sum(float(x)**2 for x in v) - 1) > 0.01 for v in result):
                self.last_error = self.last_error or "invalid_query_embedding_output"
                return []
            vectors.extend(result)
        return vectors

    def _cancel_idle_timer_locked(self):
        self._idle_generation += 1
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _schedule_idle_timer_locked(self):
        if (self._closed or self._pending_cancellations or self._process is None
                or self._idle_timeout_seconds <= 0 or self._last_used_at is None):
            return
        self._cancel_idle_timer_locked()
        delay = max(0.001, self._idle_timeout_seconds -
                    (time.monotonic() - self._last_used_at))
        timer = threading.Timer(delay, self._release_if_idle,
                                args=("idle", self._idle_generation))
        timer.name = "embeddinggemma2-idle-release"
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def get_status(self):
        """Return an observational snapshot without starting or keeping a worker alive.

        Metrics are the last response's sample, only exposed while that same
        worker is loaded. A crashed process is never reported as loaded.
        """
        with self._admission_lock:
            process = self._process
            exit_code = process.poll() if process is not None else None
            running = process is not None and exit_code is None
            loaded = running and self._initialized and not self._releasing
            pending = len(self._pending_cancellations)
            active = int(self._active_operation is not None)
            error = self.last_error
            if process is not None and not running:
                error = error or f"embedding_worker_exited (exit_code={exit_code})"
            if self._closed:
                state = "closed"
            elif self._releasing:
                state = "releasing"
            elif active and not loaded:
                state = "loading"
            elif pending:
                state = "busy"
            elif loaded:
                state = "loaded"
            else:
                state = "failed" if error else "unloaded"
            return {
                "state": state,
                "loaded": bool(loaded),
                "process_running": running,
                "pid": process.pid if running else None,
                "configured_device": self.configured_device,
                "effective_device": self.effective_device if loaded else None,
                "precision": self.precision if loaded else None,
                "fingerprint": self.fingerprint,
                "dimension": DIMENSION,
                "active_requests": active,
                "queued_requests": max(0, pending - active),
                "pending_requests": pending,
                "idle_timeout_seconds": self._idle_timeout_seconds,
                "idle_seconds": (max(0.0, time.monotonic() - self._last_used_at)
                                 if loaded and not pending and self._last_used_at is not None
                                 else None),
                "metrics": dict(self.metrics) if loaded else {},
                "metrics_source": "last_worker_response" if loaded and self.metrics else None,
                "last_release_reason": self._last_release_reason,
                "last_error": error,
            }

    def _release_if_idle(self, reason="manual", generation=None):
        # Reserve the lifecycle while admission is locked, without ever waiting
        # there. New work may queue afterwards, then lazily reload after teardown.
        with self._admission_lock:
            if self._closed:
                return {"released": False, "reason": "closed", "status": self.get_status()}
            if generation is not None and generation != self._idle_generation:
                return {"released": False, "reason": "superseded", "status": self.get_status()}
            if self._pending_cancellations or not self._lifecycle_lock.acquire(blocking=False):
                return {"released": False, "reason": "busy", "status": self.get_status()}
            self._cancel_idle_timer_locked()
            self._releasing = True
        result_reason = reason
        try:
            process = self._process
            released = process is not None and process.poll() is None
            self._stop()
            with self._admission_lock:
                if released:
                    self._last_release_reason = reason
                else:
                    result_reason = "not_loaded"
        except Exception as exc:
            released = False
            result_reason = "error"
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("EmbeddingGemma 2 worker release failed: %s", self.last_error)
        finally:
            with self._admission_lock:
                self._releasing = False
            self._lifecycle_lock.release()
        return {"released": released, "reason": result_reason,
                "status": self.get_status()}

    async def release(self):
        """Unload this private worker only when no inference is active or queued.

        Busy requests are not cancelled. The wrapper stays usable, and its
        next request lazily starts the exact same model and vector identity.
        """
        return await asyncio.to_thread(self._release_if_idle)

    @property
    def dimension(self):
        return DIMENSION

    def close(self):
        with self._admission_lock:
            self._closed = True
            self._cancel_idle_timer_locked()
            for cancelled in self._pending_cancellations:
                cancelled.set()
        # Active IPC notices cancellation and exits before teardown; queued
        # calls observe _closed and cannot resurrect a worker after close.
        with self._lifecycle_lock:
            self._stop()
        self._executor.shutdown(wait=False, cancel_futures=True)


_shared_runtime = None
_runtime_lock = threading.Lock()


def peek_embedding_runtime():
    """Return the current wrapper without creating, starting, or refreshing it."""
    with _runtime_lock:
        return _shared_runtime


def get_embedding(config):
    global _shared_runtime
    with _runtime_lock:
        if _shared_runtime is None or _shared_runtime._closed:
            _shared_runtime = EmbeddingGemma2Embedding(config)
        elif _shared_runtime.config != config:
            raise RuntimeError(
                "Conflicting embedding runtime placement; use one common RAG config"
            )
        return _shared_runtime
