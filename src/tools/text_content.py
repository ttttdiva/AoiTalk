"""Safe content-based text detection shared by model-facing file readers."""

from __future__ import annotations

import codecs
import struct
import unicodedata
from pathlib import Path


class TextContentError(ValueError):
    """Base error for bytes that must not be exposed as model-readable text."""

    error_code = "unsupported_text_encoding"


class BinaryFileError(TextContentError):
    """Raised when file bytes are binary or fail text-safety validation."""

    error_code = "binary_file"


_BINARY_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG image"),
    (b"\xff\xd8\xff", "JPEG image"),
    (b"%PDF-", "PDF document"),
    (b"PK\x03\x04", "ZIP/archive or Office package"),
    (b"PK\x05\x06", "empty ZIP archive"),
    (b"PK\x07\x08", "spanned ZIP archive"),
    (b"\x1f\x8b", "gzip archive"),
    (b"\xfd7zXZ\x00", "xz archive"),
    (b"7z\xbc\xaf'\x1c", "7z archive"),
    (b"Rar!\x1a\x07", "RAR archive"),
    (b"\x7fELF", "ELF executable"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "OLE compound document"),
    (b"SQLite format 3\x00", "SQLite database"),
)


def _binary_magic(data: bytes, *, file_size: int | None = None) -> str | None:
    """Return a binary label only for a sufficiently validated signature.

    Two-byte ASCII prefixes such as ``BM`` and ``MZ`` also occur naturally in
    prose (for example, "BM25 search documentation").  Treat those formats as
    binary only after validating their structural header fields instead of
    using the prefix alone.
    """

    actual_size = len(data) if file_size is None else file_size

    if len(data) >= 14 and data.startswith(b"BM"):
        declared_size, reserved_1, reserved_2, pixel_offset = struct.unpack_from(
            "<IHHI", data, 2
        )
        if (
            reserved_1 == 0
            and reserved_2 == 0
            and 14 <= pixel_offset <= declared_size
            and declared_size <= actual_size
        ):
            return "bitmap image"

    if len(data) >= 64 and data.startswith(b"MZ"):
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        if (
            64 <= pe_offset <= actual_size - 4
            and pe_offset + 4 <= len(data)
            and data[pe_offset : pe_offset + 4] == b"PE\x00\x00"
        ):
            return "Windows executable"

    if len(data) >= 12 and data.startswith(b"RIFF"):
        declared_payload_size = struct.unpack_from("<I", data, 4)[0]
        form_type = data[8:12]
        if (
            form_type in {b"WAVE", b"AVI ", b"WEBP", b"ACON"}
            and declared_payload_size >= 4
            and declared_payload_size + 8 <= actual_size
        ):
            return "RIFF media"

    if len(data) >= 10 and data.startswith(b"ID3"):
        version = data[3]
        size_bytes = data[6:10]
        if version in {2, 3, 4} and all(value < 0x80 for value in size_bytes):
            declared_payload_size = sum(
                value << shift for value, shift in zip(size_bytes, (21, 14, 7, 0))
            )
            if declared_payload_size + 10 <= actual_size:
                return "MP3 audio"

    if len(data) >= 13 and data[:6] in {b"GIF87a", b"GIF89a"}:
        width, height = struct.unpack_from("<HH", data, 6)
        packed = data[10]
        descriptor_offset = 13
        if packed & 0x80:
            descriptor_offset += 3 * (2 ** ((packed & 0x07) + 1))
        if (
            width > 0
            and height > 0
            and descriptor_offset < actual_size
            and descriptor_offset < len(data)
            and data[descriptor_offset] in {0x21, 0x2C, 0x3B}
        ):
            return "GIF image"

    if len(data) >= 27 and data.startswith(b"OggS") and data[4] == 0:
        segment_count = data[26]
        header_size = 27 + segment_count
        if header_size <= len(data):
            payload_size = sum(data[27:header_size])
            if header_size + payload_size <= actual_size:
                return "Ogg media"

    if len(data) >= 8 and data.startswith(b"fLaC"):
        metadata_type = data[4] & 0x7F
        metadata_size = int.from_bytes(data[5:8], "big")
        if metadata_type <= 6 and metadata_size + 8 <= actual_size:
            return "FLAC audio"

    if (
        len(data) >= 10
        and data.startswith(b"BZh")
        and data[3:4] in b"123456789"
        and data[4:10] in {b"1AY&SY", b"\x17rE8P\x90"}
    ):
        return "bzip2 archive"

    for magic, label in _BINARY_MAGICS:
        if data.startswith(magic):
            return label
    # ISO Base Media (MP4/MOV/HEIF) stores ``ftyp`` at byte offset 4.
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "ISO base media"
    return None


def _validate_decoded_text(content: str) -> None:
    if not content:
        return
    if "\x00" in content:
        raise BinaryFileError("NUL bytes are not allowed in text files")

    allowed_whitespace = {"\t", "\n", "\r", "\f"}
    if content.isascii():
        disallowed_controls = sum(
            content.count(chr(code))
            for code in (*range(1, 32), 127)
            if chr(code) not in allowed_whitespace
        )
        printable = len(content) - disallowed_controls
    else:
        disallowed_controls = 0
        printable = 0
        for char in content:
            if char.isprintable() or char in allowed_whitespace:
                printable += 1
            category = unicodedata.category(char)
            if category in {"Cc", "Cs"} and char not in allowed_whitespace:
                disallowed_controls += 1

    length = len(content)
    if disallowed_controls > max(2, int(length * 0.01)):
        raise BinaryFileError("Too many control characters for a text file")
    if printable / length < 0.85:
        raise BinaryFileError("Printable character ratio is too low for a text file")


def decode_text_bytes(
    data: bytes,
    *,
    path: str | Path | None = None,
    known_text_extensions: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, str]:
    """Decode safe text bytes and return ``(content, encoding)``.

    BOMs are authoritative and processed before NUL/control validation. UTF-8
    is accepted regardless of extension. A narrow CP932 fallback is available
    only for explicitly known text extensions; there is deliberately no
    latin-1 fallback because it can turn arbitrary binary into apparent text.
    """

    if not data:
        return "", "utf-8"

    bom_encodings = (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    )
    for bom, encoding in bom_encodings:
        if not data.startswith(bom):
            continue
        try:
            content = data[len(bom) :].decode(encoding, errors="strict")
        except UnicodeDecodeError as exc:
            raise TextContentError(f"Invalid {encoding} text") from exc
        _validate_decoded_text(content)
        return content, encoding

    magic = _binary_magic(data, file_size=len(data))
    if magic:
        raise BinaryFileError(f"Detected binary format: {magic}")

    try:
        content = data.decode("utf-8", errors="strict")
        encoding = "utf-8"
    except UnicodeDecodeError as utf8_error:
        extension = ""
        if path is not None:
            target = Path(path)
            extension = target.suffix.lower() or target.name.lower()
        if extension not in known_text_extensions:
            raise BinaryFileError("File bytes are not valid UTF-8 text") from utf8_error
        try:
            content = data.decode("cp932", errors="strict")
            encoding = "cp932"
        except UnicodeDecodeError as cp932_error:
            raise BinaryFileError("File bytes are not valid supported text") from cp932_error

    _validate_decoded_text(content)
    return content, encoding


def read_safe_text(
    path: str | Path,
    *,
    known_text_extensions: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, str]:
    """Read and safely decode a file after the caller has validated its path/size."""

    target = Path(path)
    return decode_text_bytes(
        target.read_bytes(),
        path=target,
        known_text_extensions=known_text_extensions,
    )


class _StreamingTextValidator:
    """Incremental equivalent of ``_validate_decoded_text``."""

    def __init__(self) -> None:
        self.length = 0
        self.printable = 0
        self.disallowed_controls = 0

    def feed(self, content: str) -> None:
        if "\x00" in content:
            raise BinaryFileError("NUL bytes are not allowed in text files")
        allowed_whitespace = {"\t", "\n", "\r", "\f"}
        self.length += len(content)
        if content.isascii():
            disallowed = sum(
                content.count(chr(code))
                for code in (*range(1, 32), 127)
                if chr(code) not in allowed_whitespace
            )
            self.disallowed_controls += disallowed
            self.printable += len(content) - disallowed
            return
        for char in content:
            if char.isprintable() or char in allowed_whitespace:
                self.printable += 1
            category = unicodedata.category(char)
            if category in {"Cc", "Cs"} and char not in allowed_whitespace:
                self.disallowed_controls += 1

    def finish(self) -> None:
        if not self.length:
            return
        if self.disallowed_controls > max(2, int(self.length * 0.01)):
            raise BinaryFileError("Too many control characters for a text file")
        if self.printable / self.length < 0.85:
            raise BinaryFileError("Printable character ratio is too low for a text file")


def _scan_text_lines(
    target: Path,
    *,
    encoding: str,
    skip_bytes: int,
    start_line: int,
    end_line: int | None,
    max_selected_lines: int,
    max_bytes: int,
) -> tuple[list[str], int]:
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    validator = _StreamingTextValidator()
    selected: list[str] = []
    pending = ""
    current_line = 1
    bytes_read = skip_bytes

    def consume(decoded: str, *, final: bool = False) -> None:
        nonlocal pending, current_line
        validator.feed(decoded)
        parts = (pending + decoded).split("\n")
        pending = parts.pop()
        for line in parts:
            if (
                current_line >= start_line
                and (end_line is None or current_line <= end_line)
                and len(selected) < max_selected_lines
            ):
                selected.append(line)
            current_line += 1
        if final:
            if (
                current_line >= start_line
                and (end_line is None or current_line <= end_line)
                and len(selected) < max_selected_lines
            ):
                selected.append(pending)

    with target.open("rb") as source:
        source.seek(skip_bytes)
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > max_bytes:
                raise TextContentError(
                    f"File exceeds the {max_bytes // 1024 // 1024}MB read limit"
                )
            consume(decoder.decode(chunk, final=False))
        consume(decoder.decode(b"", final=True), final=True)

    validator.finish()
    return selected, current_line


def read_safe_text_lines(
    path: str | Path,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_selected_lines: int = 101,
    max_bytes: int,
    known_text_extensions: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[str], int, str]:
    """Safely classify a file while retaining only a bounded line window.

    The complete byte stream is decoded and validated so binary content after
    the requested window cannot evade classification, but at most
    ``max_selected_lines`` lines are retained in memory.
    """

    target = Path(path)
    size = target.stat().st_size
    if size > max_bytes:
        raise TextContentError(
            f"File exceeds the {max_bytes // 1024 // 1024}MB read limit"
        )

    with target.open("rb") as source:
        header = source.read(min(size, 64 * 1024))
    if not header:
        return ([""] if start_line == 1 else []), 1, "utf-8"

    bom_encodings = (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    )
    for bom, reported_encoding in bom_encodings:
        if not header.startswith(bom):
            continue
        decoder_encoding = "utf-8" if reported_encoding == "utf-8-sig" else reported_encoding
        try:
            lines, total_lines = _scan_text_lines(
                target,
                encoding=decoder_encoding,
                skip_bytes=len(bom),
                start_line=start_line,
                end_line=end_line,
                max_selected_lines=max_selected_lines,
                max_bytes=max_bytes,
            )
        except UnicodeDecodeError as exc:
            raise TextContentError(f"Invalid {reported_encoding} text") from exc
        return lines, total_lines, reported_encoding

    magic = _binary_magic(header, file_size=size)
    if magic:
        raise BinaryFileError(f"Detected binary format: {magic}")

    try:
        lines, total_lines = _scan_text_lines(
            target,
            encoding="utf-8",
            skip_bytes=0,
            start_line=start_line,
            end_line=end_line,
            max_selected_lines=max_selected_lines,
            max_bytes=max_bytes,
        )
        return lines, total_lines, "utf-8"
    except UnicodeDecodeError as utf8_error:
        extension = target.suffix.lower() or target.name.lower()
        if extension not in known_text_extensions:
            raise BinaryFileError("File bytes are not valid UTF-8 text") from utf8_error
        try:
            lines, total_lines = _scan_text_lines(
                target,
                encoding="cp932",
                skip_bytes=0,
                start_line=start_line,
                end_line=end_line,
                max_selected_lines=max_selected_lines,
                max_bytes=max_bytes,
            )
        except UnicodeDecodeError as cp932_error:
            raise BinaryFileError("File bytes are not valid supported text") from cp932_error
        return lines, total_lines, "cp932"
