"""Isolated child process for streaming XLSX text extraction.

Launched by absolute path by ``xlsx_stream_isolated``: lxml/libxml2 run only
here, so a native crash (e.g. heap corruption, exit 0xC0000374) ends this
process and never the API server.  Output is UTF-8 JSON lines on stdout:
``{"t": text}`` batches, then exactly one ``{"end": chars}`` or ``{"error", "kind"}``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import sys
import types
from pathlib import Path

BATCH_CHARS = 64 * 1024


def _load_stream_module():
    # Import by file location: ``src`` package imports pull in unrelated heavy modules.
    package = types.ModuleType("_aoi_xlsx_docs")
    package.__path__ = [str(Path(__file__).resolve().parent)]
    sys.modules["_aoi_xlsx_docs"] = package
    return importlib.import_module("_aoi_xlsx_docs.xlsx_stream"), importlib.import_module("_aoi_xlsx_docs.office_reader")


def main(argv: list[str]) -> int:
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    # Anything a library prints must not corrupt the protocol stream.
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)

    def send(message: dict) -> None:
        out.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")

    stream, reader = _load_stream_module()
    try:
        path, expected_hash = Path(argv[1]), argv[2]
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            send({"error": "原本が索引中に変更されました", "kind": "changed"})
            return 0
        batch, size, total = [], 0, 0
        # The parent enforces stalls; time spent blocked on a slow consumer must not expire the run.
        for piece in stream.iter_xlsx_text(content, timeout=math.inf):
            batch.append(piece)
            size += len(piece)
            total += len(piece)
            if size >= BATCH_CHARS:
                send({"t": "".join(batch)})
                batch, size = [], 0
        if batch:
            send({"t": "".join(batch)})
        send({"end": total})
        return 0
    except reader.OfficeSizeLimitError as exc:
        send({"error": str(exc), "kind": "size"})
    except reader.OfficeSecurityError as exc:
        send({"error": str(exc), "kind": "security"})
    except Exception as exc:  # noqa: BLE001 - reported to the parent as a bounded failure
        send({"error": f"XLSX抽出に失敗しました({type(exc).__name__})", "kind": "failed"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
