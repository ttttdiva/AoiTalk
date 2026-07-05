"""Small helpers for chat-completions multimodal payloads."""

from __future__ import annotations

import base64
from typing import Any


def normalize_image_payloads(image_data: Any) -> list[dict[str, Any]]:
    """Return image payloads in {data, mimeType, name} shape."""
    raw_images: Any
    if isinstance(image_data, dict) and isinstance(image_data.get("images"), list):
        raw_images = image_data.get("images")
    elif isinstance(image_data, list):
        raw_images = image_data
    elif isinstance(image_data, dict):
        raw_images = [image_data]
    else:
        raw_images = []

    images: list[dict[str, Any]] = []
    for raw in raw_images:
        if not isinstance(raw, dict):
            continue
        data_url = raw.get("data") or raw.get("dataUrl")
        if not isinstance(data_url, str) or not data_url:
            continue
        images.append(
            {
                "data": data_url,
                "mimeType": raw.get("mimeType") or raw.get("mime_type"),
                "name": raw.get("name"),
            }
        )
    return images


def openai_content_parts(text: str, image_data: Any) -> str | list[dict[str, Any]]:
    images = normalize_image_payloads(image_data)
    if not images:
        return text or ""
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for image in images:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": image["data"]},
            }
        )
    return parts


def data_url_to_bytes(data_url: str) -> tuple[str, bytes]:
    """Decode a data URL, returning (mime_type, bytes)."""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        raise ValueError("data URL expected")
    header, encoded = data_url.split(",", 1)
    mime_type = header.split(";", 1)[0].removeprefix("data:") or "application/octet-stream"
    return mime_type, base64.b64decode(encoded)
