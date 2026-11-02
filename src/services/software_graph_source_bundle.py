"""Materialize pinned, authorized source artifacts into an existing workspace."""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import zipfile
from pathlib import Path
from uuid import UUID

from .local_llm_runtime_manager import validate_zip_archive_member
from .native_code_bundle_io import MAX_ARTIFACT_BYTES, MAX_ARTIFACT_FILES
from .operations_service import OperationsService


MAX_PATH_BYTES = 4096
MAX_PATH_DEPTH = 128
MAX_PREFIXES = 100_000
MAX_PREFIX_BYTES = 8 * 1024 * 1024


def _regular_single_link(info):
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def _same_file(target, data):
    try:
        info = target.lstat()
        if not _regular_single_link(info) or info.st_size != len(data):
            return False
        with target.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            return (_regular_single_link(opened) and os.path.samestat(info, opened)
                    and handle.read(len(data) + 1) == data)
    except OSError:
        return False


class SourceBundleError(ValueError):
    """A safe, content-free failure code for the caller."""


async def materialize_source_bundles(*, session, actor, target_project_id, workspace: Path,
                                     input_artifact_refs, operations: OperationsService | None = None) -> dict:
    """Read pinned ArtifactVersion DTO refs and copy ZIP files into a leased workspace.

    The caller owns workspace authorization and its exclusive lease. No provenance or
    execution status is inferred from content. Failures never disclose member names.
    """
    if input_artifact_refs is None or input_artifact_refs == [] or input_artifact_refs == ():
        return {"status": "no_inputs", "artifacts": [], "files": []}
    if not isinstance(input_artifact_refs, (list, tuple)):
        raise SourceBundleError("invalid_refs")
    try:
        project = str(UUID(str(target_project_id)))
    except (ValueError, TypeError, AttributeError):
        raise SourceBundleError("invalid_project") from None
    service = operations or OperationsService()
    artifacts, files, names = [], {}, {}
    total_compressed = total_uncompressed = total_members = total_prefix_bytes = 0
    for ref in input_artifact_refs:
        if not isinstance(ref, dict) or set(ref) - {"id", "sha256", "size_bytes"}:
            raise SourceBundleError("invalid_ref")
        try:
            artifact_id = str(UUID(str(ref["id"])))
            digest = ref["sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError
            size = ref.get("size_bytes")
            if size is not None and (type(size) is not int or size < 0):
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SourceBundleError("invalid_ref") from None
        # This is the normal actor-checked, immutable-version content read.
        metadata, raw = await service.get_artifact_content(session, actor, artifact_id)
        if str(metadata.get("project_id")) != project:
            raise SourceBundleError("project_mismatch")
        actual_hash = hashlib.sha256(raw).hexdigest()
        if (str(metadata.get("id")) != artifact_id or metadata.get("sha256") != digest
                or actual_hash != digest or metadata.get("size_bytes") != len(raw)
                or (size is not None and size != len(raw))):
            raise SourceBundleError("pin_mismatch")
        total_compressed += len(raw)
        if total_compressed > MAX_ARTIFACT_BYTES:
            raise SourceBundleError("bundle_limit")
        if not zipfile.is_zipfile(io.BytesIO(raw)):
            raise SourceBundleError("unsupported_non_zip")
        artifacts.append({"id": artifact_id, "sha256": digest, "size_bytes": len(raw)})
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = archive.infolist()
                total_members += len(members)
                if total_members > MAX_ARTIFACT_FILES:
                    raise SourceBundleError("bundle_limit")
                for member in members:
                    path = validate_zip_archive_member(member)
                    original = member.filename.replace("\\", "/")
                    if len(original.encode("utf-8")) > MAX_PATH_BYTES or original.count("/") > MAX_PATH_DEPTH:
                        raise SourceBundleError("bundle_limit")
                    parts = original.rstrip("/").split("/")
                    if len(parts) > MAX_PATH_DEPTH:
                        raise SourceBundleError("bundle_limit")
                    if (not parts or any(not p or p in {".", ".."} or p[-1:] in {".", " "}
                            or any(ord(c) < 32 or c in '<>:"|?*' for c in p)
                            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)
                            for p in parts) or str(path) != "/".join(parts)
                            or member.flag_bits & 1):
                        raise SourceBundleError("unsafe_zip")
                    mode = (member.external_attr >> 16) & 0o170000
                    if (member.is_dir() and mode not in (0, stat.S_IFDIR)) or (not member.is_dir() and mode not in (0, stat.S_IFREG)):
                        raise SourceBundleError("unsafe_zip")
                    # Track implicit directories too, rejecting case aliases and file/dir conflicts.
                    for i in range(1, len(parts) + 1):
                        name = "/".join(parts[:i]); key = name.casefold()
                        kind = "dir" if i < len(parts) or member.is_dir() else "file"
                        previous = names.get(key)
                        if previous and (previous[0] != name or previous[1] != kind or i == len(parts) and previous[2]):
                            raise SourceBundleError("unsafe_zip")
                        if previous is None:
                            total_prefix_bytes += len(name.encode("utf-8"))
                            if len(names) >= MAX_PREFIXES or total_prefix_bytes > MAX_PREFIX_BYTES:
                                raise SourceBundleError("bundle_limit")
                        names[key] = (name, kind, i == len(parts) or bool(previous and previous[2]))
                    if member.is_dir():
                        continue
                    if len(files) >= MAX_ARTIFACT_FILES or member.file_size > MAX_ARTIFACT_BYTES - total_uncompressed:
                        raise SourceBundleError("bundle_limit")
                    data = archive.read(member)
                    if len(data) != member.file_size:
                        raise SourceBundleError("unsafe_zip")
                    total_uncompressed += len(data)
                    files[str(path)] = data
        except SourceBundleError:
            raise
        except (RuntimeError, ValueError, OSError, zipfile.BadZipFile, NotImplementedError, EOFError):
            raise SourceBundleError("unsafe_zip") from None
    root = Path(workspace)
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise SourceBundleError("invalid_workspace")
    # Preflight every target before the first write; never follow existing links.
    for name, data in files.items():
        target = root
        parts = name.split("/")
        for part in parts[:-1]:
            target = target / part
            if target.is_symlink() or target.is_junction() or (target.exists() and not target.is_dir()):
                raise SourceBundleError("workspace_conflict")
        target = target / parts[-1]
        if (target.exists() or target.is_symlink() or target.is_junction()) and not _same_file(target, data):
            raise SourceBundleError("workspace_conflict")
    for name, data in files.items():
        target = root.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        created = None
        try:
            with target.open("xb") as handle:
                created = os.fstat(handle.fileno())
                if handle.write(data) != len(data):
                    raise OSError("short write")
        except FileExistsError:
            if not _same_file(target, data):
                raise SourceBundleError("workspace_conflict") from None
        except Exception:
            if created is not None:
                try:
                    current = target.lstat()
                    if (_regular_single_link(current) and os.path.samestat(created, current)
                            and not _same_file(target, data)):
                        target.unlink()
                except OSError:
                    pass
            raise SourceBundleError("workspace_write_failed") from None
    return {"status": "materialized", "artifacts": artifacts,
            "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
                      for name, data in files.items()]}
