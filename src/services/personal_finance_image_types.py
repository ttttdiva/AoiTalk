"""Personal finance 添付画像の純粋な形式/寸法ヘッダ検証。

PNG / JPEG / WebP のヘッダのみを境界付きで読む。デコード・OCR・ファイル/ネットワーク
アクセスは行わない。ヘッダ整合性の確認であり画像のサニタイズではない。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SUPPORTED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_AXIS = 16384
JPEG_SCAN_MAX_BYTES = 64 * 1024
JPEG_SCAN_MAX_MARKERS = 256

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_UNSUPPORTED_SOF = frozenset({0xC1, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


class FinanceImageTypeError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class FinanceImageType:
    mime_type: str
    width: int
    height: int
    byte_size: int


def _sniff(content: bytes) -> str | None:
    if content.startswith(_PNG_SIGNATURE):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _malformed(message: str) -> FinanceImageTypeError:
    return FinanceImageTypeError("malformed_header", message)


def _parse_png(content: bytes) -> tuple[int, int]:
    if len(content) < 24 or content[12:16] != b"IHDR":
        raise _malformed("PNG IHDR missing")
    if struct.unpack(">I", content[8:12])[0] != 13 or len(content) < 33:
        raise _malformed("PNG IHDR length")
    width, height = struct.unpack(">II", content[16:24])
    return width, height


def _parse_jpeg(content: bytes) -> tuple[int, int]:
    limit = min(len(content), JPEG_SCAN_MAX_BYTES)
    pos = 2
    for _ in range(JPEG_SCAN_MAX_MARKERS):
        while pos + 1 < limit and content[pos] == 0xFF and content[pos + 1] == 0xFF:
            pos += 1
        if pos + 2 > limit or content[pos] != 0xFF:
            raise _malformed("JPEG marker not found")
        marker = content[pos + 1]
        pos += 2
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue
        if marker in (0xD9, 0xDA):
            raise _malformed("JPEG ended before SOF")
        if pos + 2 > limit:
            raise _malformed("JPEG truncated")
        seg_len = struct.unpack(">H", content[pos : pos + 2])[0]
        if seg_len < 2:
            raise _malformed("JPEG segment length")
        if marker in (0xC0, 0xC2):
            if seg_len < 11 or pos + seg_len > limit:
                raise _malformed("JPEG SOF truncated")
            components = content[pos + 7]
            if components < 1 or seg_len != 8 + 3 * components:
                raise _malformed("JPEG SOF component table")
            height, width = struct.unpack(">HH", content[pos + 3 : pos + 7])
            return width, height
        if marker in _UNSUPPORTED_SOF:
            raise _malformed("unsupported JPEG SOF type")
        pos += seg_len
    raise _malformed("JPEG marker limit exceeded")


def _parse_webp(content: bytes) -> tuple[int, int]:
    if len(content) < 21:
        raise _malformed("WebP truncated")
    if struct.unpack("<I", content[4:8])[0] + 8 != len(content):
        raise _malformed("WebP RIFF length mismatch")
    fourcc = content[12:16]
    chunk_size = struct.unpack("<I", content[16:20])[0]
    if 20 + chunk_size > len(content):
        raise _malformed("WebP chunk exceeds file")
    if chunk_size % 2 and (20 + chunk_size >= len(content) or content[20 + chunk_size] != 0):
        raise _malformed("WebP chunk pad byte")
    if fourcc == b"VP8 ":
        if chunk_size < 10 or len(content) < 30 or content[23:26] != b"\x9d\x01\x2a":
            raise _malformed("VP8 start code")
        w, h = struct.unpack("<HH", content[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if fourcc == b"VP8L":
        if chunk_size < 5 or content[20] != 0x2F:
            raise _malformed("VP8L signature")
        bits = struct.unpack("<I", content[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if fourcc == b"VP8X":
        if chunk_size < 10 or len(content) < 30:
            raise _malformed("VP8X size")
        w = int.from_bytes(content[24:27], "little") + 1
        h = int.from_bytes(content[27:30], "little") + 1
        return w, h
    raise _malformed("unsupported WebP chunk")


_PARSERS = {"image/png": _parse_png, "image/jpeg": _parse_jpeg, "image/webp": _parse_webp}


def inspect_finance_image(
    content: bytes,
    claimed_mime: str,
    *,
    max_bytes: int = 10485760,
    max_pixels: int = 16000000,
) -> FinanceImageType:
    if type(content) is not bytes:
        raise _malformed("content must be bytes")
    mime = (claimed_mime or "").strip().lower()
    if mime not in SUPPORTED_MIME_TYPES:
        raise FinanceImageTypeError("unsupported_type", f"unsupported mime: {mime!r}")
    if len(content) > max_bytes:
        raise FinanceImageTypeError("source_too_large", "image exceeds byte limit")
    sniffed = _sniff(content)
    if sniffed is None:
        raise _malformed("unrecognized image signature")
    if sniffed != mime:
        raise FinanceImageTypeError("mime_mismatch", f"{mime} != {sniffed}")
    width, height = _PARSERS[mime](content)
    if width <= 0 or height <= 0:
        raise _malformed("non-positive dimensions")
    if width > MAX_AXIS or height > MAX_AXIS or width * height > max_pixels:
        raise FinanceImageTypeError("pixel_limit", f"{width}x{height}")
    return FinanceImageType(mime, width, height, len(content))
