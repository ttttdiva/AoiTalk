"""Disk-backed download preparation for large filer payloads."""

from __future__ import annotations

import mimetypes
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import file_explorer_service as service


@dataclass(frozen=True)
class PreparedDownload:
    """A file that can be streamed with ``FileResponse``."""

    path: Path
    filename: str
    mime_type: str
    temporary: bool = False


def _temporary_zip() -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix="aoitalk-download-",
        suffix=".zip",
        delete=False,
    ) as handle:
        return Path(handle.name)


def _build_directory_archive(source: Path) -> PreparedDownload:
    archive_path = _temporary_zip()
    archive_root_name = source.name or "workspace"
    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            service._write_directory_contents_to_archive(
                archive,
                source,
                empty_root_name=archive_root_name,
            )
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    return PreparedDownload(
        path=archive_path,
        filename=f"{archive_root_name}.zip",
        mime_type="application/zip",
        temporary=True,
    )


def prepare_download_path(path: Path) -> PreparedDownload | None:
    """Prepare an already-resolved file or directory for streaming.

    Callers that perform their own authorization (for example, the Project
    API, which resolves a project-scoped path before opening its DB session)
    can use this helper without resolving the path again against the global
    workspace root.  Directory archives are written to a temporary file;
    regular files are returned as-is so ``FileResponse`` can stream them from
    disk.
    """
    target = Path(path)
    if not target.exists():
        return None
    if target.is_dir():
        return _build_directory_archive(target)
    if not target.is_file():
        return None

    mime_type, _ = mimetypes.guess_type(str(target))
    return PreparedDownload(
        path=target,
        filename=target.name,
        mime_type=mime_type or "application/octet-stream",
    )


def prepare_download_file(
    path: str, is_admin: bool = False
) -> PreparedDownload | None:
    """Resolve a file directly or create a disk-backed ZIP for a directory."""
    target, valid = service._resolve_path(path, is_admin=is_admin)
    if not valid:
        return None
    return prepare_download_path(target)


def prepare_download_items(
    paths: Iterable[str], is_admin: bool = False
) -> PreparedDownload | None:
    """Prepare selected filer items without holding their payload in memory."""
    selected_paths = list(paths)
    if len(selected_paths) == 1:
        return prepare_download_file(selected_paths[0], is_admin=is_admin)

    resolved_items, error = service._resolve_selected_items(
        selected_paths, is_admin=is_admin
    )
    if error:
        return None

    archive_path = _temporary_zip()
    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            service._write_selected_items_to_archive(archive, resolved_items)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    return PreparedDownload(
        path=archive_path,
        filename=service._selected_archive_name(resolved_items),
        mime_type="application/zip",
        temporary=True,
    )


def remove_temp_download(path: Path) -> None:
    """Remove a generated download after the response has finished."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Cleanup must not turn an otherwise successful download into an error.
        pass
