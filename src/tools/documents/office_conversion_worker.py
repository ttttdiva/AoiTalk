"""Isolated MarkItDown worker for Office document conversion."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any


def _failure() -> dict[str, Any]:
    return {"success": False, "error": "ファイル変換に失敗しました"}


def _read_request() -> tuple[bytes, str]:
    header_line = sys.stdin.buffer.readline(64 * 1024)
    if not header_line or not header_line.endswith(b"\n"):
        raise ValueError("invalid conversion request")
    header = json.loads(header_line[:-1].decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("invalid conversion request")
    filename = header.get("filename")
    size = header.get("size")
    if not isinstance(filename, str) or not filename or len(filename) > 4096:
        raise ValueError("invalid conversion request")
    if type(size) is not int or size < 0:
        raise ValueError("invalid conversion request")
    content = sys.stdin.buffer.read(size)
    if len(content) != size:
        raise ValueError("truncated conversion request")
    return content, filename


@contextlib.contextmanager
def _suppress_parser_output():
    """Suppress Python and native fd writes while third-party code runs."""

    saved_stdout = os.dup(1)
    sink_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(sink_fd, 1)
        os.close(sink_fd)
        sink_fd = -1
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
    finally:
        if sink_fd >= 0:
            os.close(sink_fd)
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)


def _write_result(result: object) -> None:
    try:
        from .office_reader import _safe_conversion_result
    except ImportError:
        # The worker is launched by absolute path so importing the parent
        # ``src.tools`` package cannot trigger unrelated tool side effects.
        from office_reader import _safe_conversion_result

    normalized = _safe_conversion_result(result)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    encoded = payload.encode("utf-8")
    output = getattr(sys.stdout, "buffer", None)
    if output is None:
        sys.stdout.write(payload)
        sys.stdout.flush()
    else:
        output.write(encoded)
        output.flush()


def main() -> int:
    try:
        content, filename = _read_request()
        # Third-party Office readers occasionally write diagnostics to either
        # stream.  Suppress those streams while importing and parsing so the
        # stdout IPC channel remains one bounded JSON document.
        with _suppress_parser_output():
            try:
                from .office_reader import _convert_office_in_process
            except ImportError:
                from office_reader import _convert_office_in_process

            result = _convert_office_in_process(content, filename)
    except ImportError:
        result = {"success": False, "error": "markitdown がインストールされていません"}
    except BaseException:
        result = _failure()
    _write_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
