"""Private embedding-only stdin/stdout worker; no network listener or path API."""

from __future__ import annotations
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time

# Library progress/logging must never corrupt the line-oriented protocol.
PROTOCOL_OUTPUT = sys.stdout
sys.stdout = sys.stderr
from embedding_identity import (
    DIMENSION,
    EMBEDDING_FINGERPRINT,
    MODEL_ID,
    MODEL_REVISION,
)
from embedding_text import (
    embedding_request_diagnostics,
    escape_text_only_media_placeholders,
    sanitize_query_embedding_input,
)

MAX_REQUEST_BYTES = 128 * 1024 * 1024
model = None
model_settings = None


def metrics():
    import psutil
    import torch
    from importlib.metadata import version, PackageNotFoundError

    versions = {}
    for package in (
        "sentence-transformers",
        "transformers",
        "torch",
        "torchvision",
        "torchaudio",
        "av",
        "huggingface-hub",
    ):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    memory = psutil.Process().memory_info()
    return {
        "rss_bytes": memory.rss,
        "peak_rss_bytes": getattr(memory, "peak_wset", memory.rss),
        "cuda_peak_bytes": torch.cuda.max_memory_allocated()
        if torch.cuda.is_available()
        else None,
        "versions": versions,
    }


def initialize(settings):
    global model, model_settings
    if model is not None:
        if settings != model_settings:
            raise ValueError("worker_configuration_changed")
        return
    import torch
    from sentence_transformers import SentenceTransformer
    from importlib.metadata import version
    from packaging.version import Version

    for package, minimum in [
        ("sentence-transformers", "6.1.0"),
        ("transformers", "5.19.0"),
    ]:
        if Version(version(package)) < Version(minimum):
            raise RuntimeError(f"{package}>={minimum} required")
    device = settings.get("device", "auto")
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("configured_cuda_unavailable")
    dtype = (
        torch.bfloat16
        if device.startswith("cuda") and torch.cuda.is_bf16_supported()
        else torch.float32
    )
    started = time.perf_counter()
    model = SentenceTransformer(
        MODEL_ID,
        revision=MODEL_REVISION,
        device=device,
        cache_folder=settings.get("cache_dir"),
        local_files_only=settings.get("local_files_only", True),
        trust_remote_code=False,
        model_kwargs={"torch_dtype": dtype},
    )
    if model.get_sentence_embedding_dimension() != DIMENSION:
        model = None
        raise RuntimeError("embedding_dimension_mismatch")
    model_settings = settings
    model._aoitalk_device = device
    model._aoitalk_precision = str(dtype)
    model._aoitalk_load_seconds = time.perf_counter() - started


def unpack_media(item, temporary, *, query=False):
    import numpy as np
    from PIL import Image

    result = {}
    if not isinstance(item, dict) or set(item) - ({"text", "image", "audio", "video"} if query else {"image", "audio", "video"}):
        raise ValueError("unsupported_media_keys")
    for modality, packed in item.items():
        if query and modality == "text":
            if not isinstance(packed, str):
                raise ValueError("invalid_query_text")
            result[modality] = packed
            continue
        if query and isinstance(packed, list):
            if not packed or len(packed) > 16:
                raise ValueError("invalid_query_media_list")
            result[modality] = [unpack_media({modality: value}, temporary)[modality] for value in packed]
            if len(result[modality]) == 1:
                result[modality] = result[modality][0]
            continue
        if not isinstance(packed, dict) or packed.get("encoding") != "base64":
            raise ValueError("media_bytes_required")
        raw = base64.b64decode(packed["data"], validate=True)
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError("media_chunk_too_large")
        if modality == "image":
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 40_000_000:
                    raise ValueError("image_dimensions_too_large")
                result[modality] = image.convert("RGB").copy()
        elif modality == "audio":
            values = np.frombuffer(raw, dtype="<f4").copy()
            if not 0 < len(values) <= 480000 or not np.isfinite(values).all():
                raise ValueError("invalid_audio_chunk")
            result[modality] = values
        elif modality == "video":
            # Decode only bytes supplied by the authorized host; never open a
            # client pathname, URL, file:// URI or remote video from IPC.
            path = Path(temporary) / f"clip-{len(list(Path(temporary).iterdir()))}.mkv"
            path.write_bytes(raw)
            result[modality] = str(path)
    if not result:
        raise ValueError("empty_media")
    return result


QUERY_PROCESSING = {"text": {"truncation": False}, "video": {"fps": 1, "max_frames": 32}}


def validate_query(inputs, maximum):
    """Count the official processor's expanded shared sequence, before forward."""
    if type(maximum) is not int or not 1 <= maximum <= 8192:
        raise ValueError("invalid_query_token_budget")
    counts = []
    for item in inputs:
        item = sanitize_query_embedding_input(item)
        prompt = model.prompts.get("SearchQuery") if isinstance(item, str) else ""
        if isinstance(item, str) and not prompt:
            raise RuntimeError("official_search_query_prompt_missing")
        features = model.preprocess([item], prompt=prompt, processing_kwargs=QUERY_PROCESSING)
        mask = features.get("attention_mask")
        if mask is None:
            raise RuntimeError("query_processor_token_count_unavailable")
        count = int(mask[0].sum())
        if count > maximum:
            raise ValueError("query_token_budget_exceeded")
        counts.append(count)
    return counts


def execute(request):
    import numpy as np

    if request.get("identity") != EMBEDDING_FINGERPRINT:
        raise ValueError("embedding_identity_mismatch")
    op = request.get("op")
    if op == "initialize":
        initialize(request["settings"])
        return {
            "dimension": DIMENSION,
            "device": model._aoitalk_device,
            "precision": model._aoitalk_precision,
            "load_seconds": model._aoitalk_load_seconds,
            "metrics": metrics(),
        }
    if op not in {"encode", "validate_query"} or model is None:
        raise ValueError("worker_not_initialized")
    mode = request.get("mode")
    inputs = request.get("inputs")
    if not isinstance(inputs, list) or len(inputs) > 32:
        raise ValueError("invalid_embedding_batch")
    temporary_root = Path(
        os.environ.get("AOITALK_EMBEDDING_TMP_DIR")
        or Path(__file__).resolve().parents[2] / ".local/eg2-worker-media"
    )
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="eg2-media-", dir=temporary_root
    ) as directory:
        if op == "validate_query" or mode == "query":
            inputs = [
                sanitize_query_embedding_input(
                    item if isinstance(item, str) else unpack_media(item, directory, query=True)
                )
                for item in inputs
            ]
            counts = validate_query(inputs, request.get("max_tokens", 8192))
            if op == "validate_query":
                return {"token_counts": counts}
        elif mode == "media":
            inputs = [unpack_media(item, directory) for item in inputs]
        elif mode not in {"document", "SearchQuery", "CodeRetrieval"} or any(
            not isinstance(x, str) for x in inputs
        ):
            raise ValueError("invalid_text_request")
        else:
            inputs = [escape_text_only_media_placeholders(item) for item in inputs]
        options = dict(
            batch_size=max(1, min(32, int(request.get("batch_size", 8)))),
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        if mode in {"SearchQuery", "CodeRetrieval"}:
            options["prompt_name"] = mode
        else:
            options["prompt"] = ""
        started = time.perf_counter()
        if mode == "query":
            vectors = np.empty((len(inputs), DIMENSION), dtype=np.float32)
            for text_only in (True, False):
                indices = [i for i, value in enumerate(inputs) if isinstance(value, str) == text_only]
                if not indices:
                    continue
                query_options = {k: v for k, v in options.items() if k not in {"prompt", "prompt_name"}}
                query_options["processing_kwargs"] = QUERY_PROCESSING
                query_options.update({"prompt_name": "SearchQuery"} if text_only else {"prompt": ""})
                vectors[indices] = model.encode([inputs[i] for i in indices], **query_options)
        else:
            vectors = model.encode(inputs, **options)
        if (
            vectors.shape != (len(inputs), DIMENSION)
            or not np.isfinite(vectors).all()
            or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=0.005)
        ):
            raise RuntimeError("invalid_embedding_output")
        return {
            "vectors": vectors.tolist(),
            "inference_seconds": time.perf_counter() - started,
            "metrics": metrics(),
        }


def main():
    while True:
        line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
        if not line:
            break
        request_id = None
        request = None
        diagnostics = embedding_request_diagnostics(request)
        try:
            if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise ValueError("request_too_large")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("invalid_embedding_request")
            request_id = request.get("request_id")
            diagnostics = embedding_request_diagnostics(request)
            result = execute(request)
            response = {
                "request_id": request_id,
                "identity": EMBEDDING_FINGERPRINT,
                "ok": True,
                **result,
            }
        except Exception as exc:
            identity_mismatch = (
                isinstance(exc, ValueError)
                and str(exc) == "embedding_identity_mismatch"
            )
            malformed_envelope = (
                not isinstance(request, dict)
                and isinstance(exc, (AttributeError, TypeError, ValueError))
            )
            is_request_input_error = (
                malformed_envelope
                or (
                    isinstance(request, dict)
                    and request.get("op") in {"encode", "validate_query"}
                    and isinstance(exc, (ValueError, TypeError))
                    and not identity_mismatch
                )
            )
            has_unpaired_image_placeholder = (
                diagnostics["special_token_counts"]["image"] > 0
                and diagnostics["image_count"] == 0
            )
            response = {
                "request_id": request_id,
                "identity": EMBEDDING_FINGERPRINT,
                "ok": False,
                "error_category": "request" if is_request_input_error else "worker",
                "error_code": (
                    "textual_multimodal_placeholder_without_image"
                    if is_request_input_error and has_unpaired_image_placeholder
                    else "embedding_input_rejected"
                    if is_request_input_error
                    else "embedding_worker_runtime_error"
                ),
                "error_type": type(exc).__name__,
                "diagnostics": diagnostics,
            }
        PROTOCOL_OUTPUT.write(json.dumps(response, separators=(",", ":")) + "\n")
        PROTOCOL_OUTPUT.flush()


if __name__ == "__main__":
    main()
