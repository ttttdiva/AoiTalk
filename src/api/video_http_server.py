#!/usr/bin/env python3
"""Loopback-only HTTP video server for explicitly enabled local playback."""

from __future__ import annotations

import ipaddress
import logging
import os
import stat
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

logger = logging.getLogger(__name__)

try:
    from ..tools.absolute_filer_paths import get_filer_mime_type

    VIDEO_PATH_SUPPORT_AVAILABLE = True
except ImportError:
    VIDEO_PATH_SUPPORT_AVAILABLE = False
    get_filer_mime_type = None


class _UnsafeVideoPath(ValueError):
    """Raised when an untrusted path crosses the video server boundary."""


def is_loopback_host(host: str) -> bool:
    """Accept only literal loopback addresses and the exact localhost name."""
    value = str(host or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _is_link_or_reparse(path: Path) -> bool:
    """Return true for POSIX symlinks and Windows junction/reparse entries."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        item_stat = path.lstat()
        return bool(
            getattr(item_stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError):
        # Inspection failures must never turn into permission to serve a file.
        return True


def _absolute_lexical_path(value: str | os.PathLike[str]) -> Path:
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise _UnsafeVideoPath("絶対パスのみ指定できます")
        return Path(os.path.abspath(os.fspath(candidate)))
    except _UnsafeVideoPath:
        raise
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise _UnsafeVideoPath("パスを解決できません") from exc


def _reject_link_components(root: Path, relative: Path) -> None:
    current = root
    for component in relative.parts:
        current = current / component
        if component.casefold().endswith(".lnk") or _is_link_or_reparse(current):
            raise _UnsafeVideoPath(
                "symlink、reparse point、shortcut経由のアクセスは許可されません"
            )


def _trusted_root(value: str | os.PathLike[str]) -> Path | None:
    """Return an existing non-link directory as a canonical allowed root."""
    try:
        lexical = _absolute_lexical_path(value)
        if any(part.casefold().endswith(".lnk") for part in lexical.parts):
            return None
        current = Path(lexical.anchor)
        for component in lexical.parts[1:]:
            current = current / component
            if _is_link_or_reparse(current):
                return None
        if not lexical.is_dir():
            return None
        return lexical.resolve(strict=True)
    except (OSError, RuntimeError, _UnsafeVideoPath):
        return None


def _default_allowed_roots() -> list[Path]:
    def configured_root(value: str | os.PathLike[str]) -> Path:
        # StorageContext resolves AOITALK_WORKSPACES_DIR relative to the
        # process working directory.  Match that canonical configuration
        # boundary here before the untrusted request-path validator (which
        # intentionally rejects relative paths) sees the root.
        return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))

    workspace_root = os.environ.get("AOITALK_WORKSPACES_DIR") or "./workspaces"
    roots = [configured_root(workspace_root)]
    filer_root = os.environ.get("FILER_ROOT_PATH")
    if filer_root:
        roots.append(configured_root(filer_root))
    return roots


def _normalize_allowed_roots(
    roots: Iterable[str | os.PathLike[str]] | None,
) -> tuple[Path, ...]:
    trusted: list[Path] = []
    seen: set[str] = set()
    for raw_root in roots if roots is not None else _default_allowed_roots():
        root = _trusted_root(raw_root)
        if root is None:
            logger.warning("Video HTTP server ignored an unsafe allowed root")
            continue
        key = os.path.normcase(os.fspath(root))
        if key not in seen:
            trusted.append(root)
            seen.add(key)
    return tuple(trusted)


def _resolve_video_file(path: str, allowed_roots: tuple[Path, ...]) -> Path | None:
    """Resolve an existing regular file below one trusted root, without aliases."""
    lexical = _absolute_lexical_path(path)
    if any(part.casefold().endswith(".lnk") for part in lexical.parts):
        raise _UnsafeVideoPath("shortcut経由のアクセスは許可されません")

    for root in allowed_roots:
        try:
            relative = lexical.relative_to(root)
        except ValueError:
            continue
        _reject_link_components(root, relative)
        try:
            resolved = lexical.resolve(strict=True)
            resolved.relative_to(root)
        except FileNotFoundError:
            return None
        except (OSError, RuntimeError, ValueError) as exc:
            raise _UnsafeVideoPath("許可rootの外側へアクセスできません") from exc
        if _is_link_or_reparse(resolved):
            raise _UnsafeVideoPath("link経由のアクセスは許可されません")
        return resolved if resolved.is_file() else None

    raise _UnsafeVideoPath("許可rootの外側へアクセスできません")


def _normalize_cors_origins(origins: Iterable[str] | None) -> list[str]:
    """Keep only explicit HTTP(S) loopback origins; wildcard is never accepted."""
    normalized: list[str] = []
    for raw_origin in origins or ():
        origin = str(raw_origin or "").strip().rstrip("/")
        try:
            parsed = urlsplit(origin)
            valid = (
                origin != "*"
                and parsed.scheme in {"http", "https"}
                and parsed.hostname is not None
                and is_loopback_host(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            )
            # Accessing .port also rejects malformed/out-of-range ports.
            _ = parsed.port
        except ValueError:
            valid = False
        if valid and origin not in normalized:
            normalized.append(origin)
        elif origin:
            logger.warning("Video HTTP server ignored an unsafe CORS origin")
    return normalized


def create_video_http_app(
    *,
    allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
    allowed_origins: Iterable[str] | None = None,
) -> FastAPI:
    """Create the unauthenticated helper app with a narrow local filesystem scope."""
    app = FastAPI(title="AoiTalk Video Server (HTTP)")
    trusted_roots = _normalize_allowed_roots(allowed_roots)
    trusted_origins = _normalize_cors_origins(allowed_origins)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=trusted_origins,
        allow_credentials=False,
        allow_methods=["GET", "HEAD", "OPTIONS"],
        allow_headers=["Range"],
        expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
    )

    @app.get("/api/filer/file")
    async def serve_video_file(path: str, request: Request):
        """Serve a root-contained video file with Range request support."""
        request_origin = request.headers.get("origin")
        if request_origin is not None and request_origin not in trusted_origins:
            raise HTTPException(status_code=403, detail="Origin is not allowed")
        if not VIDEO_PATH_SUPPORT_AVAILABLE:
            raise HTTPException(status_code=503, detail="Video path support is unavailable")
        try:
            file_path = _resolve_video_file(path, trusted_roots)
        except _UnsafeVideoPath as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if file_path is None:
            raise HTTPException(status_code=404, detail="File not found")

        mime_type = get_filer_mime_type(file_path)
        if not mime_type.startswith("video/"):
            raise HTTPException(
                status_code=403,
                detail="Only video files are allowed on this endpoint",
            )

        file_size = file_path.stat().st_size
        range_header = request.headers.get("range")
        if not range_header:
            return FileResponse(
                path=str(file_path),
                media_type=mime_type,
                headers={"Accept-Ranges": "bytes"},
            )

        try:
            range_str = range_header.replace("bytes=", "")
            range_parts = range_str.split("-")
            start = int(range_parts[0]) if range_parts[0] else 0
            end = int(range_parts[1]) if range_parts[1] else file_size - 1
        except (ValueError, IndexError):
            return FileResponse(
                path=str(file_path),
                media_type=mime_type,
                headers={"Accept-Ranges": "bytes"},
            )

        if start >= file_size:
            raise HTTPException(status_code=416, detail="Range not satisfiable")
        end = min(end, file_size - 1)
        content_length = end - start + 1

        def file_iterator():
            chunk_size = 1024 * 1024
            with open(file_path, "rb") as file_handle:
                file_handle.seek(start)
                remaining = content_length
                while remaining > 0:
                    data = file_handle.read(min(chunk_size, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        encoded_filename = quote(file_path.name, safe="")
        return StreamingResponse(
            file_iterator(),
            status_code=206,
            media_type=mime_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}",
            },
        )

    @app.get("/health")
    async def health_check():
        return {"status": "ok", "service": "video-http"}

    return app


async def run_video_http_server(
    host: str,
    port: int,
    *,
    allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
    allowed_origins: Iterable[str] | None = None,
):
    """Run the helper server only on a loopback bind."""
    if not is_loopback_host(host):
        raise ValueError("Video HTTP server must bind to a loopback host")

    import uvicorn

    app = create_video_http_app(
        allowed_roots=allowed_roots,
        allowed_origins=allowed_origins,
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    logger.info("Starting HTTP video server on http://%s:%s", host, port)
    await server.serve()
