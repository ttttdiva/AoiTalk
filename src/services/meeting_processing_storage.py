from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .clip_ingest_storage import (
    ClipIngestStorage,
    ClipUpload,
    ClipUploadError,
)


MIB = 1024 * 1024

MEETING_AUDIO_MAX_BYTES = 500 * MIB
MEETING_AUDIO_MAX_DURATION_SECONDS = 8 * 60 * 60

_ALLOWED_SUFFIXES = frozenset(
    {
        ".wav",
        ".mp3",
        ".m4a",
        ".mp4",
        ".mpeg",
        ".mpga",
        ".webm",
        ".ogg",
        ".flac",
    }
)

# Keep the existing public name used by the API/readiness response and tests.
MEETING_AUDIO_SUFFIXES = _ALLOWED_SUFFIXES


class MeetingAudioStorage(ClipIngestStorage):
    """Durable staging for meeting-processing source audio."""

    def __init__(
        self,
        workspace_root: str | os.PathLike[str] | None = None,
        *,
        max_upload_bytes: int | None = None,
        defer_staging_cleanup: bool = True,
    ) -> None:
        if max_upload_bytes is None:
            try:
                configured = int(
                    os.environ.get(
                        "AOITALK_MEETING_PROCESSING_MAX_AUDIO_BYTES",
                        MEETING_AUDIO_MAX_BYTES,
                    )
                )
            except (TypeError, ValueError):
                configured = MEETING_AUDIO_MAX_BYTES
        else:
            configured = max(1, int(max_upload_bytes))

        # Deployment config may lower the limit but must not silently widen
        # the v1 wire contract beyond 500 MiB.
        configured = min(configured, MEETING_AUDIO_MAX_BYTES)

        super().__init__(
            workspace_root,
            max_upload_bytes=configured,
            max_files=1,
            defer_staging_cleanup=defer_staging_cleanup,
        )

    def staging_namespace(self) -> str:
        return "meeting-processing"

    @staticmethod
    def validate_file_name(file_name: Any) -> str:
        name = str(file_name or "").strip()

        if not name:
            raise ClipUploadError("meeting audio file name is empty")

        # The base storage strips path traversal while staging and resolving.
        # Meeting processing additionally requires a supported media suffix
        # every time durable metadata is materialized.
        suffix = Path(name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise ClipUploadError(
                f"unsupported meeting audio suffix: {suffix or '<none>'}"
            )

        return name

    async def stage_upload(self, user_id: Any, upload: Any) -> ClipUpload:
        self.validate_file_name(getattr(upload, "filename", None))

        staged = await super().stage_upload(user_id, upload)

        # Validate again after the base storage has normalized the filename.
        self.validate_file_name(staged.file_name)
        return staged

    def resolve_upload(self, user_id: Any, upload_id: Any) -> ClipUpload:
        resolved = super().resolve_upload(user_id, upload_id)

        # This is intentionally repeated on every durable read.  A staging
        # sidecar is untrusted mutable filesystem state after the request that
        # originally created it.
        self.validate_file_name(resolved.file_name)
        return resolved
