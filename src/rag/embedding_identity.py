"""Immutable identity of every AoiTalk dense embedding and media unit."""

import hashlib
import json

MODEL_ID = "google/embeddinggemma-2"
MODEL_REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
DIMENSION = 768
EMBEDDING_IDENTITY = {
    "model_id": MODEL_ID,
    "revision": MODEL_REVISION,
    "dimension": DIMENSION,
    "query_prefix": "task: search result | query: ",
    "document_format": "title: {title} | text: {content}",
    "pooling": "official-sentence-transformers-modules",
    "normalize": "l2",
    "precision": "bf16-native-cuda-otherwise-fp32",
    "image_prefix": None,
    "image_frames": "all-frames-rgb",
    "pdf": "page-image-and-page-text",
    "pdf_dpi": 144,
    "audio_sample_rate": 16000,
    "audio_chunk_samples": 480000,
    "audio_preparation": "resampled-pcm-full-sample-count",
    "video_fps": 1,
    "video_chunk_frames": 32,
    "video_preparation": "trim-reset-timestamps-fps1-ceil-ffv1",
    "video_soundtrack": "separate-audio-intervals",
    "input_version": 1,
}
EMBEDDING_FINGERPRINT = hashlib.sha256(
    json.dumps(EMBEDDING_IDENTITY, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def embedding_collection_name(base: str) -> str:
    """Keep source/ACL partitions, isolate this model's exact vector space."""
    suffix = "__eg2_" + EMBEDDING_FINGERPRINT[:16]
    return base if base.endswith(suffix) else base + suffix


def document_input(content: str, title: str | None = None) -> str:
    return f"title: {title or 'none'} | text: {content}"
