"""CPU-only sampled visual evidence from already-authorized managed video bytes.

These frames do not establish full motion/audio review or producer authorship.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .media_asset_service import VIDEO_MAX_BYTES

_MAX_DIMENSION = 1024
_TIMEOUT_SECONDS = 30
_MIMES = {"video/mp4", "video/webm", "video/quicktime"}


class VideoReviewFrameError(ValueError):
    """Invalid input or unavailable/failed local video decoding."""


@dataclass(frozen=True)
class VideoReviewFrame:
    data: bytes
    mime_type: str
    timestamp_seconds: float
    source_sha256: str
    frame_sha256: str


def _run(argv: list[str]) -> bytes:
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=_TIMEOUT_SECONDS,
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )
    except FileNotFoundError as exc:
        raise VideoReviewFrameError("Video decoder or probe is unavailable") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise VideoReviewFrameError("Video decoder or probe failed") from exc
    return result.stdout


def extract_video_review_frames(
    data: bytes,
    *,
    expected_sha256: str,
    mime_type: str,
    max_frames: int = 3,
) -> tuple[VideoReviewFrame, ...]:
    """Extract up to three sampled JPEGs; timestamps are requested seek positions."""
    if not isinstance(data, bytes) or not data or len(data) > VIDEO_MAX_BYTES:
        raise VideoReviewFrameError("Invalid video byte length")
    if type(max_frames) is not int or not 1 <= max_frames <= 3:
        raise VideoReviewFrameError("max_frames must be between 1 and 3")
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise VideoReviewFrameError("Invalid source SHA256")
    source_sha256 = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(source_sha256, expected_sha256):
        raise VideoReviewFrameError("Source SHA256 mismatch")
    if mime_type not in _MIMES:
        raise VideoReviewFrameError("Unsupported video MIME type")
    head = data[:512]
    if mime_type == "video/webm":
        valid = head.startswith(b"\x1aE\xdf\xa3") and b"webm" in head
    else:
        valid = (
            len(head) >= 16
            and head[4:8] == b"ftyp"
            and int.from_bytes(head[:4], "big") >= 16
            and (head[8:12] == b"qt  ") == (mime_type == "video/quicktime")
        )
    if not valid:
        raise VideoReviewFrameError("Video bytes do not match MIME type")

    with tempfile.TemporaryDirectory(prefix="aoitalk-video-review-") as directory:
        path = Path(directory) / "input.video"
        path.write_bytes(data)
        output = _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
        try:
            duration = float(output.strip())
        except ValueError as exc:
            raise VideoReviewFrameError("Invalid video duration") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise VideoReviewFrameError("Invalid video duration")
        # Leave a small margin for the last decodable frame, including short clips.
        end = max(0.0, duration - min(0.5, duration * 0.1))
        times = (
            (0.0,)
            if max_frames == 1
            else ((0.0, end) if max_frames == 2 else (0.0, duration / 2, end))
        )
        frames = []
        for timestamp in times:
            jpeg = _run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-hwaccel",
                    "none",
                    "-ss",
                    str(timestamp),
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={_MAX_DIMENSION}:{_MAX_DIMENSION}:force_original_aspect_ratio=decrease",
                    "-f",
                    "image2",
                    "-vcodec",
                    "mjpeg",
                    "pipe:1",
                ]
            )
            try:
                with Image.open(io.BytesIO(jpeg)) as image:
                    if (
                        image.format != "JPEG"
                        or min(image.size) < 1
                        or max(image.size) > _MAX_DIMENSION
                    ):
                        raise ValueError("Invalid JPEG dimensions or format")
                    image.verify()
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise VideoReviewFrameError("Decoder produced an invalid JPEG") from exc
            frames.append(
                VideoReviewFrame(
                    jpeg,
                    "image/jpeg",
                    timestamp,
                    source_sha256,
                    hashlib.sha256(jpeg).hexdigest(),
                )
            )
        return tuple(frames)
