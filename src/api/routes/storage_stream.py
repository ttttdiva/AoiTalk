"""Bounded download streaming without putting remote I/O in the shared ASGI pool."""
from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from ...services.storage_files import open_download
from ...services.storage_io import StorageUnavailable, check_io_cancelled, storage_io
from ...services.storage_roots import StorageRoot


def byte_range(header: str | None, size: int) -> tuple[int, int, bool]:
    if not header:
        return 0, max(-1, size - 1), False
    try:
        if not re.fullmatch(r"bytes=[0-9]*-[0-9]*", header):
            raise ValueError()
        first, last = header[6:].split("-", 1)
        if not first:
            suffix = int(last)
            if suffix <= 0:
                raise ValueError()
            start, end = max(0, size - suffix), size - 1
        else:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        if size == 0 or start < 0 or start >= size or end < start:
            raise ValueError()
        return start, end, True
    except (ValueError, TypeError) as exc:
        raise HTTPException(416, "無効なRangeです", headers={"Content-Range": f"bytes */{size}"}) from exc


async def storage_download_response(root: StorageRoot, path: str, request: Request, *, inline: bool = False):
    def prepare():
        try:
            handle, info, mime = open_download(root, path)
        except OSError as exc:
            raise StorageUnavailable("ストレージからファイルを取得できません") from exc
        with handle:
            start, end, partial = byte_range(request.headers.get("range"), info.st_size)
            check_io_cancelled()
            return info, mime, start, end, partial
    info, mime, start, end, partial = await storage_io.run(root.io_key, prepare)
    remaining = max(0, end - start + 1)
    headers = {
        "Content-Length": str(remaining), "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(path.rsplit('/', 1)[-1], safe='')}",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{info.st_size}"

    async def stream():
        offset = start
        remaining_bytes = remaining
        while remaining_bytes:
            # Open/read/close stay in one bounded worker. A timed-out open can
            # never return an orphaned descriptor to an abandoned coroutine.
            def read_chunk():
                handle, current, _mime = open_download(root, path)
                with handle:
                    if (current.st_ino, current.st_size, current.st_mtime_ns) != (info.st_ino, info.st_size, info.st_mtime_ns):
                        raise StorageUnavailable("配信中にファイルが更新されました")
                    handle.seek(offset)
                    block = handle.read(min(1024 * 1024, remaining_bytes))
                    check_io_cancelled()
                    return block
            block = await storage_io.run(root.io_key, read_chunk)
            if not block:
                raise StorageUnavailable("ファイル配信が中断されました")
            remaining_bytes -= len(block)
            offset += len(block)
            yield block

    if inline and not mime.startswith(("image/png", "image/jpeg", "image/webp", "image/gif", "video/", "audio/")):
        mime = "application/octet-stream"
        headers["Content-Disposition"] = headers["Content-Disposition"].replace("inline;", "attachment;", 1)
    return StreamingResponse(stream(), status_code=206 if partial else 200,
                             media_type=mime, headers=headers)
