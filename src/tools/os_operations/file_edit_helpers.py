"""Pure helpers for ``FileEditor.str_replace``.

They locate the target region without rewriting any other part of the file and
build feedback that lets the caller pick the next operation (widen the anchor,
re-read a line range, or stop because the edit is already applied).
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_REPORTED_MATCHES = 8
MAX_DIFF_LINES = 60
_PREVIEW_CHARS = 120
_TAB_SIZE = 8


@dataclass
class Match:
    start: int
    end: int
    start_line: int
    end_line: int


@dataclass
class Resolution:
    """Matches in the original text plus how ``old_str``/``new_str`` were adapted."""

    matches: List[Match]
    old: str
    new: str
    notes: List[str] = field(default_factory=list)


def line_of(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _preview(text: str) -> str:
    text = text.rstrip("\r\n")
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "..."


def _find_all(content: str, needle: str) -> List[Match]:
    matches: List[Match] = []
    if not needle:
        return matches
    pos = content.find(needle)
    while pos != -1:
        end = pos + len(needle)
        matches.append(
            Match(pos, end, line_of(content, pos), line_of(content, max(pos, end - 1)))
        )
        pos = content.find(needle, end)
    return matches


def _expand_with_map(content: str) -> Tuple[str, Dict[int, int]]:
    """``str.expandtabs`` plus a map from expanded offsets to original offsets.

    Only offsets on a character boundary of the original are mapped, so a match
    that would start or end inside an expanded tab can be rejected.
    """

    out: List[str] = []
    boundary: Dict[int, int] = {}
    column = 0
    expanded_len = 0
    for index, char in enumerate(content):
        boundary[expanded_len] = index
        if char == "\t":
            width = _TAB_SIZE - (column % _TAB_SIZE)
            out.append(" " * width)
            expanded_len += width
            column += width
        else:
            out.append(char)
            expanded_len += 1
            column = 0 if char in "\r\n" else column + 1
    boundary[expanded_len] = len(content)
    return "".join(out), boundary


def _tab_normalized_matches(content: str, old: str) -> Tuple[List[Match], int]:
    """Matches that agree with ``old`` once tabs are expanded (original offsets).

    Returns the matches plus the number of candidates dropped because they cut
    through a tab.
    """

    expanded, boundary = _expand_with_map(content)
    needle = old.expandtabs(_TAB_SIZE)
    matches: List[Match] = []
    rejected = 0
    if not needle:
        return matches, rejected
    pos = expanded.find(needle)
    while pos != -1:
        end = pos + len(needle)
        start_o, end_o = boundary.get(pos), boundary.get(end)
        if start_o is None or end_o is None:
            rejected += 1
        else:
            matches.append(
                Match(
                    start_o,
                    end_o,
                    line_of(content, start_o),
                    line_of(content, max(start_o, end_o - 1)),
                )
            )
        pos = expanded.find(needle, end)
    return matches, rejected


def _uses_crlf(content: str) -> bool:
    return content.count("\r\n") > 0 and content.count("\r\n") >= content.count("\n") - content.count("\r\n")


def _to_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def resolve_matches(content: str, old: str, new: str) -> Resolution:
    """Find ``old`` in ``content`` without altering the file's own bytes.

    Order: exact, then CRLF-adapted (file is CRLF, argument was LF), then
    tab-normalized (the pre-existing tolerance, now applied to the match only).
    """

    exact = _find_all(content, old)
    if exact:
        return Resolution(exact, old, new)

    candidates: List[Tuple[str, str, List[str]]] = []
    if "\n" in old and "\r\n" not in old and _uses_crlf(content):
        candidates.append(
            (_to_crlf(old), _to_crlf(new), ["matched after converting LF to the file's CRLF line endings"])
        )
    candidates.append((old, new, []))

    for cand_old, cand_new, notes in candidates:
        if notes:
            found = _find_all(content, cand_old)
            if found:
                return Resolution(found, cand_old, cand_new, notes)
        if "\t" in content or "\t" in cand_old:
            found, rejected = _tab_normalized_matches(content, cand_old)
            if found:
                notes = notes + [
                    "matched after tab normalization (tabs/spaces differed); "
                    "only the matched region was changed, tabs elsewhere are untouched and "
                    "new_str was written as given"
                ]
                return Resolution(found, cand_old, cand_new, notes)
            if rejected and not notes:
                return Resolution([], old, new, [
                    f"{rejected} tab-normalized candidate(s) were rejected because they begin or end inside a tab"
                ])
    return Resolution([], old, new)


def _line_table(content: str) -> List[str]:
    return content.split("\n")


def describe_missing(content: str, old: str, new: str, extra_notes: Optional[List[str]] = None) -> str:
    """Specific guidance for a missing anchor."""

    parts: List[str] = ["old_str was not found in the file; no changes were made."]
    parts.extend(extra_notes or [])

    if new and new != old and _find_all(content, new) and len(_find_all(content, new)) == 1:
        at = _find_all(content, new)[0].start_line
        parts.append(
            f"new_str already appears once (line {at}); this edit may already be applied. "
            "Re-read that region with read_file(start_line, end_line) before editing again."
        )

    file_lines = _line_table(content)
    old_lines = [ln.rstrip("\r") for ln in old.split("\n")]
    stripped_file = [ln.strip() for ln in file_lines]
    wanted = [ln for ln in (l.strip() for l in old_lines) if ln]

    if not wanted:
        parts.append("old_str is empty or whitespace only; provide the exact text to replace.")
        return "\n".join(parts)

    # Longest run of consecutive (whitespace-insensitive) line matches.
    first = wanted[0]
    best: Optional[Tuple[int, int]] = None  # (start index, matched count)
    for idx, line in enumerate(stripped_file):
        if line != first:
            continue
        count = 0
        j = idx
        for want in (l.strip() for l in old_lines):
            if not want:
                continue
            while j < len(stripped_file) and not stripped_file[j]:
                j += 1
            if j < len(stripped_file) and stripped_file[j] == want:
                count += 1
                j += 1
            else:
                break
        if best is None or count > best[1]:
            best = (idx, count)

    if best is not None:
        idx, count = best
        if count == len(wanted):
            parts.append(
                f"The lines exist at line {idx + 1} when whitespace is ignored, so only indentation, "
                "trailing spaces, tabs or blank lines differ. Copy the exact text from read_file(start_line, end_line) "
                f"(lines {idx + 1}-{idx + len(old_lines)})."
            )
        else:
            nonblank_seen = 0
            exp = ""
            for l in old_lines:
                if l.strip():
                    if nonblank_seen == count:
                        exp = l
                        break
                    nonblank_seen += 1
            j = idx
            consumed = 0
            while j < len(stripped_file) and consumed < count:
                if stripped_file[j]:
                    consumed += 1
                j += 1
            while j < len(stripped_file) and not stripped_file[j]:
                j += 1
            actual = file_lines[j] if j < len(file_lines) else "<end of file>"
            parts.append(
                f"The first {count} non-blank line(s) match at line {idx + 1}, but line {j + 1} differs.\n"
                f"  expected: {_preview(exp)!r}\n"
                f"  actual:   {_preview(actual)!r}"
            )
    else:
        close = difflib.get_close_matches(first, stripped_file, n=3, cutoff=0.6)
        if close:
            shown = []
            for text in close:
                ln = stripped_file.index(text) + 1
                shown.append(f"  line {ln}: {_preview(file_lines[ln - 1])!r}")
            parts.append(
                f"The first line of old_str ({_preview(first)!r}) is not in the file. Closest lines:\n"
                + "\n".join(shown)
                + "\nRe-read the region with read_file(start_line, end_line) and copy the current text."
            )
        else:
            parts.append(
                f"The first line of old_str ({_preview(first)!r}) does not resemble any line. "
                "The file may have changed; use read_file(start_line, end_line) or a search tool to locate the target."
            )
    return "\n".join(parts)


def describe_ambiguous(content: str, matches: List[Match]) -> str:
    lines = _line_table(content)
    shown = []
    for m in matches[:MAX_REPORTED_MATCHES]:
        span = f"line {m.start_line}" if m.start_line == m.end_line else f"lines {m.start_line}-{m.end_line}"
        shown.append(f"  {span}: {_preview(lines[m.start_line - 1])!r}")
    more = len(matches) - MAX_REPORTED_MATCHES
    tail = f"\n  ... and {more} more" if more > 0 else ""
    return (
        f"old_str matches {len(matches)} places; nothing was changed. "
        "Include neighbouring lines (the line before or after) so the anchor is unique.\n"
        + "\n".join(shown)
        + tail
    )


def build_diff(path: str, before: str, after: str) -> str:
    diff = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=2,
        )
    )
    if len(diff) > MAX_DIFF_LINES:
        omitted = len(diff) - MAX_DIFF_LINES
        diff = diff[:MAX_DIFF_LINES] + [f"... ({omitted} more diff lines omitted)"]
    return "\n".join(diff)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def syntax_note(path: str, before: str, after: str) -> str:
    """Advisory only: report a syntax error newly introduced by the edit."""

    suffix = path.lower().rsplit(".", 1)[-1] if "." in path else ""

    def check(text: str) -> Optional[str]:
        try:
            if suffix == "py":
                ast.parse(text)
            elif suffix == "json":
                json.loads(text)
            else:
                return None
        except SyntaxError as exc:
            return f"line {exc.lineno}: {exc.msg}"
        except json.JSONDecodeError as exc:
            return f"line {exc.lineno}: {exc.msg}"
        except ValueError as exc:  # e.g. null bytes in source
            return str(exc)
        return None

    if suffix not in {"py", "json"}:
        return ""
    now = check(after)
    if now is None:
        return ""
    if check(before) is not None:
        return f"Syntax check: the file still has a syntax error ({now}); it was already invalid before this edit."
    return (
        f"Syntax check warning: this edit leaves the file with a syntax error ({now}). "
        "Fine if more edits are coming; otherwise fix it or use undo_edit."
    )
