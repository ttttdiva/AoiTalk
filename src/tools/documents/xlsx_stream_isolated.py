"""Run the streaming XLSX extractor in a killable child process.

lxml/libxml2 must not parse untrusted workbooks inside the API server: a
native fault (heap corruption, Windows 0xC0000374) would take the server down.
The child streams text batches over a pipe, so neither the workbook nor the
extracted text is ever held whole by the caller, and a crash surfaces as
``OfficeWorkerCrashError`` for the indexer to record against that one source.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from .office_reader import OfficeSecurityError, OfficeSizeLimitError, _terminate_and_reap, _windows_process_options

XLSX_STREAM_WORKER_PATH = str(Path(__file__).resolve().with_name("xlsx_stream_worker.py"))
# Longest the child may go without producing the next batch (the consumer's own pauses don't count).
XLSX_STREAM_STALL_SECONDS = 300.0
# One protocol line: a 64 KiB text batch escaped to JSON stays far below this.
XLSX_STREAM_LINE_MAX_BYTES = 2 * 1024 * 1024


class OfficeWorkerCrashError(OfficeSecurityError):
    """The extractor process died or stalled; the server itself is unaffected."""


class OfficeSourceChangedError(OfficeSecurityError):
    """The original no longer matches the hash the index run started from."""


def _exit_description(code: int | None) -> str:
    if code is None:
        return "unknown"
    return f"0x{code & 0xFFFFFFFF:08X}" if code < 0 or code > 255 else str(code)


def iter_xlsx_text_isolated(path: Path | str, sha256: str) -> Iterator[str]:
    """Yield the workbook text produced by an isolated child process."""
    options = _windows_process_options()
    options["stdin"] = subprocess.DEVNULL
    if os.name == "nt":
        # The index must never starve the interactive server of CPU.
        options["creationflags"] |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
    process = subprocess.Popen([sys.executable, "-u", XLSX_STREAM_WORKER_PATH, str(path), sha256], **options)
    stalled = threading.Event()
    finished = False

    def read_line() -> bytes:
        def kill() -> None:
            stalled.set()
            try:
                process.kill()
            except OSError:
                pass

        timer = threading.Timer(XLSX_STREAM_STALL_SECONDS, kill)
        timer.daemon = True
        timer.start()
        try:
            return process.stdout.readline(XLSX_STREAM_LINE_MAX_BYTES + 1)
        finally:
            timer.cancel()

    try:
        while True:
            line = read_line()
            if not line:
                break
            if len(line) > XLSX_STREAM_LINE_MAX_BYTES or not line.endswith(b"\n"):
                raise OfficeWorkerCrashError("XLSX抽出プロセスの出力が不正です")
            try:
                message = json.loads(line)
            except ValueError as exc:
                raise OfficeWorkerCrashError("XLSX抽出プロセスの出力が不正です") from exc
            if "t" in message:
                yield message["t"]
            elif "end" in message:
                finished = True
                break
            else:
                error = str(message.get("error") or "XLSXを読み取れません")
                kind = message.get("kind")
                if kind == "size":
                    raise OfficeSizeLimitError(error)
                if kind == "changed":
                    raise OfficeSourceChangedError(error)
                raise OfficeSecurityError(error)
        if not finished:
            _terminate_and_reap(process)
            if stalled.is_set():
                raise OfficeWorkerCrashError("XLSX抽出プロセスが応答しませんでした")
            raise OfficeWorkerCrashError(
                f"XLSX抽出プロセスが異常終了しました(exit {_exit_description(process.returncode)})"
            )
    finally:
        # Also reached on GeneratorExit when a consumer stops early.
        _terminate_and_reap(process)
