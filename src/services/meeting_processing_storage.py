from __future__ import annotations

import os
from pathlib import Path
import stat
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
        create_root: bool = True,
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

        try:
            super().__init__(
                workspace_root,
                max_upload_bytes=configured,
                max_files=1,
                defer_staging_cleanup=defer_staging_cleanup,
                create_root=create_root,
            )
        except ClipUploadError as exc:
            if getattr(exc, "reason", "source_unavailable") == "source_unavailable":
                reason = (
                    "original_path_invalid"
                    if any(token in str(exc).lower() for token in ("symlink", "reparse", "シンボリック"))
                    else "original_storage_failure"
                )
                raise ClipUploadError(
                    str(exc), reason=reason
                ) from exc
            raise

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

    def _probe_original_paths(self, user_id: Any, upload_id: Any) -> None:
        """Classify the current root/child state immediately before a read."""
        try:
            root_info = self.workspace_root.stat()
            if not stat.S_ISDIR(root_info.st_mode):
                raise OSError("meeting storage root is not a directory")
            with os.scandir(self.workspace_root):
                pass
            try:
                upload_dir = self._upload_dir(user_id, upload_id)
            except ClipUploadError as exc:
                message = str(exc).lower()
                if any(token in message for token in ("symlink", "reparse", "シンボリック")):
                    raise ClipUploadError(
                        "meeting audio original path is invalid",
                        reason="original_path_invalid",
                    ) from exc
                raise
            for path in (upload_dir / "metadata.json", upload_dir / "payload"):
                try:
                    info = path.lstat()
                except FileNotFoundError as exc:
                    raise ClipUploadError(
                        "meeting audio original is missing", reason="original_missing"
                    ) from exc
                except OSError as exc:
                    raise ClipUploadError(
                        "meeting audio storage is unavailable",
                        reason="original_storage_failure",
                    ) from exc
                if not stat.S_ISREG(info.st_mode):
                    raise ClipUploadError(
                        "meeting audio original path is invalid",
                        reason="original_path_invalid",
                    )
        except ClipUploadError:
            raise
        except (OSError, RuntimeError) as exc:
            raise ClipUploadError(
                "meeting audio storage is unavailable",
                reason="original_storage_failure",
            ) from exc

    def resolve_upload(self, user_id: Any, upload_id: Any) -> ClipUpload:
        # Resolve the durable namespace before opening metadata.  This keeps a
        # disconnected/unreadable root retryable while a missing child upload
        # remains a terminal original gap.
        self._probe_original_paths(user_id, upload_id)
        try:
            resolved = super().resolve_upload(user_id, upload_id)
        except ClipUploadError as exc:
            # A GC may remove a child between the probe and the base reader.
            # Reprobe the current root and child so that this race remains a
            # typed missing/storage failure instead of a terminal generic gap.
            if getattr(exc, "reason", "source_unavailable") == "source_unavailable":
                try:
                    self._probe_original_paths(user_id, upload_id)
                except ClipUploadError as reprobed:
                    raise reprobed from exc
            raise

        # This is intentionally repeated on every durable read.  A staging
        # sidecar is untrusted mutable filesystem state after the request that
        # originally created it.
        self.validate_file_name(resolved.file_name)
        return resolved
