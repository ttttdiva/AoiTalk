"""Bounded streaming text extraction for XLSX workbooks too large to buffer.

``office_reader`` converts a whole workbook to one Markdown string, which is
rejected once a worksheet XML expands past its archive/output limits.  Large
but well-formed workbooks (tens of MB of decorated, mostly empty cells) are
read here one row at a time instead: every non-empty cell value is emitted as
``<cell ref>=<value>``, empty/styled-only cells are skipped, and nothing is
silently truncated -- exceeding a limit raises instead of dropping data.

Encrypted/IRM packages are not ZIP containers and are refused by the archive
preflight; there is no password or decryption path.
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from collections.abc import Iterator
from typing import IO

from lxml import etree

from .office_reader import OfficeSecurityError, OfficeSizeLimitError, _preflight_zip_archive

STREAM_MAX_MEMBERS = 10_000
STREAM_MAX_MEMBER_BYTES = 512 * 1024 * 1024
STREAM_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
STREAM_MAX_SHARED_STRING_CHARS = 64 * 1024 * 1024
STREAM_MAX_OUTPUT_CHARS = 256 * 1024 * 1024
STREAM_TIMEOUT_SECONDS = 600.0

# Transitional and Strict Open XML use different namespaces; match by local name.
_REL_ID_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
_ANY = "{*}"
_WHITESPACE = re.compile(r"\s+")


def _parse_events(source: IO[bytes], tag: str):
    return etree.iterparse(
        source, events=("end",), tag=tag,
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False,
    )


def _drop_processed(element) -> None:
    element.clear()
    parent = element.getparent()
    if parent is not None:
        while element.getprevious() is not None:
            del parent[0]


def _member_name(target: str) -> str:
    target = target.replace("\\", "/")
    return target.lstrip("/") if target.startswith("/") else f"xl/{target}"


def _sheet_members(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(sheet name, worksheet member) in workbook order."""
    with archive.open("xl/workbook.xml") as stream:
        workbook = etree.parse(stream, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    with archive.open("xl/_rels/workbook.xml.rels") as stream:
        rels = etree.parse(stream, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
    targets = {rel.get("Id"): rel.get("Target") for rel in rels.getroot().iter(f"{_ANY}Relationship")}
    result = []
    for sheet in workbook.getroot().iter(f"{_ANY}sheet"):
        rel_id = next((sheet.get(f"{{{ns}}}id") for ns in _REL_ID_NAMESPACES if sheet.get(f"{{{ns}}}id")), None)
        target = targets.get(rel_id)
        if target:
            result.append((sheet.get("name") or "", _member_name(target)))
    return result


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    strings: list[str] = []
    chars = 0
    with archive.open("xl/sharedStrings.xml") as stream:
        for _, item in _parse_events(stream, f"{_ANY}si"):
            text = _plain_text(item)
            chars += len(text)
            if chars > STREAM_MAX_SHARED_STRING_CHARS:
                raise OfficeSizeLimitError("共有文字列のサイズが上限を超えています")
            strings.append(text)
            _drop_processed(item)
    return strings


def _plain_text(item) -> str:
    # Phonetic guides (rPh) are annotations, not cell text.
    return "".join(node.text or "" for node in item.iter(f"{_ANY}t")
                   if etree.QName(node.getparent()).localname != "rPh")


def _cell_value(cell, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return _plain_text(cell)
    value = cell.find(f"{_ANY}v")
    if value is None or value.text is None:
        return ""
    if kind == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError) as exc:
            raise OfficeSecurityError("XLSXの共有文字列参照が不正です") from exc
    if kind == "b":
        return "TRUE" if value.text.strip() == "1" else "FALSE"
    return value.text


def iter_xlsx_text(content: bytes, *, timeout: float | None = None) -> Iterator[str]:
    """Yield the workbook's non-empty cell text, one sheet heading/row at a time."""
    _preflight_zip_archive(
        content,
        max_members=STREAM_MAX_MEMBERS,
        max_member_bytes=STREAM_MAX_MEMBER_BYTES,
        max_total_bytes=STREAM_MAX_TOTAL_BYTES,
    )
    deadline = time.monotonic() + (STREAM_TIMEOUT_SECONDS if timeout is None else timeout)
    emitted = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        try:
            sheets = _sheet_members(archive)
            shared = _shared_strings(archive)
        except (KeyError, etree.XMLSyntaxError) as exc:
            raise OfficeSecurityError("XLSXのブック構造を読み取れません") from exc
        if not sheets:
            raise OfficeSecurityError("XLSXにシートが見つかりません")
        for name, member in sheets:
            heading = f"## {name}\n"
            emitted += len(heading)
            yield heading
            try:
                stream = archive.open(member)
            except KeyError as exc:
                raise OfficeSecurityError("XLSXのシートを読み取れません") from exc
            with stream:
                try:
                    for _, row in _parse_events(stream, f"{_ANY}row"):
                        if time.monotonic() > deadline:
                            raise OfficeSizeLimitError("XLSX抽出がタイムアウトしました")
                        parts = []
                        for cell in row.iter(f"{_ANY}c"):
                            text = _WHITESPACE.sub(" ", _cell_value(cell, shared)).strip()
                            if text:
                                ref = cell.get("r") or ""
                                parts.append(f"{ref}={text}" if ref else text)
                        _drop_processed(row)
                        if parts:
                            line = " | ".join(parts) + "\n"
                            emitted += len(line)
                            if emitted > STREAM_MAX_OUTPUT_CHARS:
                                raise OfficeSizeLimitError("XLSX抽出結果のサイズが上限を超えています")
                            yield line
                except etree.XMLSyntaxError as exc:
                    raise OfficeSecurityError("XLSXのシートを読み取れません") from exc


def should_stream_xlsx(content: bytes) -> bool:
    """True when the ZIP directory alone exceeds the buffered converter's size limits.

    Workbooks the buffered converter can handle keep that route (and its unit
    ids), so already indexed files stay readable; only containers it refuses
    on size are streamed.
    """
    try:
        _preflight_zip_archive(content)
    except OfficeSizeLimitError:
        return True
    except OfficeSecurityError:
        return False
    return False
