"""Structured dry-run/import helpers for TRPG rulebooks and supplements.

Input files are ingestion material only. The importer stores structured rule
items or creature entries with source spans and short excerpts, not the full
OCR file body as canonical database text.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select

from ..memory.database import get_db_session
from ..models.ecc_models import (
    TRPGCreatureEntry,
    TRPGMechanicRuleLink,
    TRPGReferenceDocument,
    TRPGRuleItem,
)
from .trpg_rule_reference_service import (
    infer_rule_domain_and_mechanic,
    normalize_reference_name,
)
from .trpg_rules import list_coc_mechanic_keys, normalize_ruleset_key


_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_OCR_SUSPECT_RE = re.compile(r"(�|□|[A-Za-z]{1,2}縺|繧|譁|蜷|逕|豁|螳|驥)")
_STAT_RE = re.compile(r"\b(STR|CON|POW|DEX|APP|SIZ|INT|EDU|HP|MP)\s*[:：]?\s*(\d+)", re.I)
_SAN_RE = re.compile(r"\bSAN\b|正気度", re.I)
_SAN_LOSS_RE = re.compile(r"(?:SAN|正気度|正気度喪失)[^\n]{0,120}?([0-9]+(?:D[0-9]+)?/[0-9]+(?:D[0-9]+)?|[0-9]+/[0-9]+D[0-9]+)", re.I)
_ATTACK_HINT_RE = re.compile(r"攻撃|attack|ダメージ|damage|噛み|爪|武器", re.I)
_SPELL_HINT_RE = re.compile(r"呪文|spell|magic", re.I)
_CREATURE_ABILITY_MARKER_RE = re.compile(r"能力値\s+ロール\s+平均値")
_FIXED_STAT_LINE_RE = re.compile(r"^STR\s+.+\bCON\b.+\bSIZ\b", re.M)
_CREATURE_SECTION_CLASSIFICATION_RE = re.compile(
    r"^(?:(?:下級|上級)の(?:奉仕種族|独立種族)|唯一の存在|大いなるもの|"
    r"グレート・オールド・ワン|外なる神|旧き神|神格|化身|.+の化身)$"
)
_ENGLISH_SECTION_TITLE_RE = re.compile(r"^[A-Z][A-Za-z0-9 '&=:/().,\-]+$")
_JAPANESE_TEXT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_STAT_KEY_RE = re.compile(r"\b(STR|CON|SIZ|INT|POW|DEX|APP|EDU|SAN|HP|MP)\b", re.I)
_HIRAGANA_RE = re.compile(r"^[\u3040-\u309f]")
_RULE_TITLE_KEYWORDS = (
    "技能",
    "戦闘",
    "正気度",
    "SAN",
    "狂気",
    "呪文",
    "装備",
    "神格",
    "奉仕種族",
    "独立種族",
)
_RULE_TITLE_PREFIXES = (
    "基本",
    "探索者",
    "キーパー",
    "技能",
    "戦闘",
    "正気度",
    "SAN",
    "狂気",
    "呪文",
    "装備",
    "抵抗",
    "成長",
    "表",
)


def _safe_excerpt(text: str, limit: int = 1200) -> str:
    body = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    return body[:limit].rstrip()


def _source_span(start_line: int, end_line: int, char_start: int, char_end: int, source_label: str) -> Dict[str, Any]:
    return {
        "source_label": source_label,
        "start_line": start_line,
        "end_line": end_line,
        "char_start": char_start,
        "char_end": char_end,
    }


def _looks_like_plain_heading(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 80:
        return False
    if _HIRAGANA_RE.match(text):
        return False
    if re.search(r"[、。]", text):
        return False
    if re.search(r"\d", text):
        return False
    if text.endswith(("。", ".", "、", ",")):
        return False
    if re.match(r"^(\d+[\.)．]|第.+章|第.+節)", text):
        return True
    if text.isupper() and len(text) >= 3:
        return True
    if len(text) <= 36 and (
        text.startswith(_RULE_TITLE_PREFIXES)
        or text in _RULE_TITLE_KEYWORDS
        or re.fullmatch(r"[A-Za-z][A-Za-z /:()_-]{2,50}", text)
    ):
        return True
    return False


def _is_suspect_rule_title(title: str, body: str = "") -> bool:
    text = str(title or "").strip()
    if not text:
        return True
    if len(text) > 56:
        return True
    if _HIRAGANA_RE.match(text):
        return True
    if re.search(r"[。]", text):
        return True
    if "、" in text and not re.search(r"[:：]|（|\\(", text):
        return True
    if re.search(r"(?:ため|ところ|こと|もの|よう|だった|している|できるかぎ)$", text):
        return True
    combined = f"{text}\n{body}"
    if re.search(r"<!--\s*page:", combined, re.I) and not re.search(r"[:：]|^第.+章", text):
        return True
    return False


def _iter_sections(source_text: str) -> Iterable[Dict[str, Any]]:
    lines = str(source_text or "").splitlines()
    offsets: List[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line) + 1

    current: Optional[Dict[str, Any]] = None
    body: List[str] = []

    def finish(end_line: int) -> Optional[Dict[str, Any]]:
        nonlocal current, body
        if current is None:
            return None
        text = "\n".join(body).strip()
        item = {
            **current,
            "body": text,
            "end_line": max(current["start_line"], end_line),
            "char_end": offsets[min(max(end_line - 1, 0), len(offsets) - 1)] if offsets else 0,
        }
        current = None
        body = []
        return item

    for idx, line in enumerate(lines, start=1):
        heading = ""
        level = 1
        match = _MARKDOWN_HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            heading = match.group(2).strip()
        elif _looks_like_plain_heading(line):
            heading = line.strip()

        if heading:
            previous = finish(idx - 1)
            if previous:
                yield previous
            char_start = offsets[idx - 1] if idx - 1 < len(offsets) else 0
            current = {
                "title": heading,
                "heading_level": level,
                "start_line": idx,
                "char_start": char_start,
            }
            body = []
        elif current is not None:
            body.append(line)

    previous = finish(len(lines))
    if previous:
        yield previous


def _split_fallback_sections(source_text: str, source_label: str) -> List[Dict[str, Any]]:
    body = str(source_text or "").strip()
    if not body:
        return []
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", body) if chunk.strip()]
    sections: List[Dict[str, Any]] = []
    cursor = 0
    for index, chunk in enumerate(chunks, start=1):
        start = body.find(chunk, cursor)
        cursor = start + len(chunk)
        title = chunk.splitlines()[0].strip()[:80] or f"Item {index}"
        sections.append(
            {
                "title": title,
                "heading_level": 1,
                "start_line": body[:start].count("\n") + 1,
                "end_line": body[: start + len(chunk)].count("\n") + 1,
                "char_start": start,
                "char_end": start + len(chunk),
                "body": "\n".join(chunk.splitlines()[1:]).strip(),
            }
        )
    return sections


def _sections(source_text: str, source_label: str) -> List[Dict[str, Any]]:
    parsed = list(_iter_sections(source_text))
    if parsed:
        return parsed
    return _split_fallback_sections(source_text, source_label)


def _line_offsets(lines: Sequence[str]) -> List[int]:
    offsets: List[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line) + 1
    return offsets


def _line_number_for_offset(offsets: Sequence[int], char_pos: int) -> int:
    return max(1, bisect.bisect_right(offsets, max(0, char_pos)))


def _is_noise_title_line(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if text.startswith(("<!--", "[判読不能]", "「", "――")):
        return True
    if re.fullmatch(r"\d+", text):
        return True
    if text in {"Malleus Monstrorum", "クトゥルフ神話のクリーチャー", "クトゥルフ神話の神格"}:
        return True
    if text in {"能力値", "武器", "装甲", "呪文", "技能", "正気度喪失"}:
        return True
    return False


def _line_start(offsets: Sequence[int], line_index: int) -> int:
    if not offsets:
        return 0
    return offsets[min(max(line_index, 0), len(offsets) - 1)]


def _title_before_line(lines: Sequence[str], offsets: Sequence[int], marker_line_index: int) -> Tuple[str, int, int]:
    usable: List[Tuple[int, str]] = []
    index = marker_line_index - 1
    while index >= 0 and len(usable) < 3:
        line = lines[index].strip()
        if not line:
            if usable:
                break
            index -= 1
            continue
        if _is_noise_title_line(line):
            index -= 1
            continue
        usable.append((index, line))
        if "、" in line or "," in line or line.startswith("■"):
            break
        index -= 1
    if not usable:
        return "", marker_line_index, _line_start(offsets, marker_line_index)

    usable.reverse()
    last_index, last_line = usable[-1]
    if len(usable) >= 2:
        prev_index, prev_line = usable[-2]
        prev_is_ruby = bool(re.fullmatch(r"[\u3040-\u309f]{1,8}", prev_line))
        if not prev_is_ruby and (prev_line.endswith(("、", "=", "＝")) or "、" in prev_line or len(last_line) <= 12):
            joined = "".join(line for _, line in usable[-2:])
            return joined.strip(), prev_index, _line_start(offsets, prev_index)
    return last_line, last_index, _line_start(offsets, last_index)


def _previous_content_lines(
    lines: Sequence[str],
    start_index: int,
    lower_bound_index: int,
    limit: int = 4,
) -> List[Tuple[int, str]]:
    usable: List[Tuple[int, str]] = []
    index = start_index
    while index >= lower_bound_index and len(usable) < limit:
        line = lines[index].strip()
        if not line:
            if usable:
                break
            index -= 1
            continue
        if _is_noise_title_line(line):
            index -= 1
            continue
        usable.append((index, line))
        index -= 1
    usable.reverse()
    return usable


def _is_creature_classification_line(value: str) -> bool:
    text = str(value or "").strip()
    if not text or len(text) > 60:
        return False
    if text.startswith("クトゥルフ神話の"):
        return False
    if _STAT_KEY_RE.search(text):
        return False
    return bool(_CREATURE_SECTION_CLASSIFICATION_RE.search(text))


def _looks_like_english_section_title(value: str) -> bool:
    text = str(value or "").strip()
    if not text or _is_noise_title_line(text):
        return False
    return bool(_ENGLISH_SECTION_TITLE_RE.fullmatch(text))


def _loose_reference_name(value: str) -> str:
    text = normalize_reference_name(value)
    text = text.translate(
        str.maketrans(
            {
                "ァ": "ア",
                "ィ": "イ",
                "ゥ": "ウ",
                "ェ": "エ",
                "ォ": "オ",
                "ッ": "ツ",
                "ャ": "ヤ",
                "ュ": "ユ",
                "ョ": "ヨ",
                "ぁ": "あ",
                "ぃ": "い",
                "ぅ": "う",
                "ぇ": "え",
                "ぉ": "お",
                "っ": "つ",
                "ゃ": "や",
                "ゅ": "ゆ",
                "ょ": "よ",
            }
        )
    )
    return re.sub(r"\s+", "", text)


def _name_appears_in_title_candidates(name: str, candidates: Sequence[Tuple[int, str]]) -> bool:
    normalized_name = _loose_reference_name(_primary_creature_name(name))
    if not normalized_name:
        return False
    candidate_text = _loose_reference_name(" ".join(line for _, line in candidates))
    return normalized_name in candidate_text or candidate_text in normalized_name


def _trim_title_candidates_to_name(
    name: str,
    candidates: Sequence[Tuple[int, str]],
) -> List[Tuple[int, str]]:
    normalized_name = _loose_reference_name(_primary_creature_name(name))
    if not normalized_name:
        return list(candidates)
    for index, (_, line) in enumerate(candidates):
        if normalized_name in _loose_reference_name(line):
            start = index
            if index > 0 and _looks_like_english_section_title(candidates[index - 1][1]):
                start = index - 1
            return list(candidates[start:])
    return list(candidates)


def _fallback_section_start_by_name(
    lines: Sequence[str],
    offsets: Sequence[int],
    marker_line_index: int,
    lower_bound_index: int,
    fallback_title: str,
    fallback_title_line: int,
) -> Tuple[str, int, int]:
    normalized_name = _loose_reference_name(_primary_creature_name(fallback_title))
    if not normalized_name:
        return fallback_title, fallback_title_line, _line_start(offsets, fallback_title_line)
    search_start = max(lower_bound_index, marker_line_index - 120)
    for index in range(search_start, max(search_start, fallback_title_line)):
        line = lines[index].strip()
        if line.startswith("*"):
            continue
        if _is_noise_title_line(line):
            continue
        if normalized_name in _loose_reference_name(line):
            return fallback_title, index, _line_start(offsets, index)
    return fallback_title, fallback_title_line, _line_start(offsets, fallback_title_line)


def _section_start_before_marker(
    lines: Sequence[str],
    offsets: Sequence[int],
    marker_line_index: int,
    lower_bound_index: int,
    fallback_title: str,
    fallback_title_line: int,
) -> Tuple[str, int, int]:
    lower_bound = max(0, lower_bound_index)
    for index in range(marker_line_index - 1, lower_bound - 1, -1):
        if not _is_creature_classification_line(lines[index]):
            continue
        title_candidates = _previous_content_lines(lines, index - 1, lower_bound)
        if not title_candidates:
            continue
        if not _name_appears_in_title_candidates(fallback_title, title_candidates):
            continue
        title_candidates = _trim_title_candidates_to_name(fallback_title, title_candidates)
        title_index, title = next(
            ((line_index, line) for line_index, line in title_candidates if _JAPANESE_TEXT_RE.search(line)),
            title_candidates[-1],
        )
        start_index = title_index
        for line_index, line in title_candidates:
            if line_index == title_index or _looks_like_english_section_title(line):
                start_index = min(start_index, line_index)
        return title, start_index, _line_start(offsets, start_index)

    return _fallback_section_start_by_name(
        lines,
        offsets,
        marker_line_index,
        lower_bound,
        fallback_title,
        fallback_title_line,
    )


def _primary_creature_name(title: str) -> str:
    text = re.sub(r"^[■□\s]+", "", str(title or "").strip())
    text = re.sub(r"\s+", "", text)
    text = re.split(r"[、,]", text, maxsplit=1)[0]
    return text[:120] or str(title or "").strip()[:120]


def _valid_creature_catalog_title(title: str) -> bool:
    text = str(title or "").strip()
    name = _primary_creature_name(text)
    if len(name) < 2 or len(name) > 120:
        return False
    if not _JAPANESE_TEXT_RE.search(name):
        return False
    if _STAT_KEY_RE.search(name):
        return False
    if any(token in name for token in ("能力値", "正気度喪失", "平均ダメージ", "クトゥルフ神話の")):
        return False
    return True


def _chapter_kind_for_position(source_text: str, char_pos: int) -> str:
    deity_chapter_start = source_text.find("Deities of the Mythos")
    if deity_chapter_start < 0:
        deity_chapter_start = source_text.find("<!-- page: 0127 -->")
    post_deity_starts = [
        pos
        for pos in [
            source_text.find("<!-- page: 0265 -->", max(0, deity_chapter_start + 1)),
            source_text.find("Animals", max(0, deity_chapter_start + 1)),
        ]
        if pos >= 0
    ]
    next_creature_chapter = min(post_deity_starts) if post_deity_starts else -1
    if deity_chapter_start >= 0 and char_pos >= deity_chapter_start and (
        next_creature_chapter < 0 or char_pos < next_creature_chapter
    ):
        return "deity"
    return "creature"


def _iter_malleus_creature_sections(source_text: str, source_label: str) -> List[Dict[str, Any]]:
    text = str(source_text or "")
    lines = text.splitlines()
    offsets = _line_offsets(lines)
    candidates: List[Dict[str, Any]] = []
    mythos_catalog_end = text.find("<!-- page: 0265 -->")

    def add_candidate(marker_pos: int, marker_kind: str) -> None:
        if mythos_catalog_end >= 0 and marker_pos >= mythos_catalog_end:
            return
        marker_line = _line_number_for_offset(offsets, marker_pos) - 1
        title, title_line, title_pos = _title_before_line(lines, offsets, marker_line)
        if not _valid_creature_catalog_title(title):
            return
        candidates.append(
            {
                "title": title,
                "name": _primary_creature_name(title),
                "marker_pos": marker_pos,
                "marker_line": marker_line,
                "marker_kind": marker_kind,
                "stat_title": title,
                "stat_title_line": title_line,
                "stat_title_pos": title_pos,
            }
        )

    for match in _CREATURE_ABILITY_MARKER_RE.finditer(text):
        add_candidate(match.start(), "ability_table")
    for match in _FIXED_STAT_LINE_RE.finditer(text):
        add_candidate(match.start(), "fixed_stats")

    candidates.sort(key=lambda item: item["marker_pos"])
    deduped: List[Dict[str, Any]] = []
    for item in candidates:
        previous = deduped[-1] if deduped else None
        if previous and item["name"] == previous["name"] and item["marker_pos"] - previous["marker_pos"] < 300:
            continue
        deduped.append(item)

    for index, item in enumerate(deduped):
        previous_marker_line = deduped[index - 1]["marker_line"] if index > 0 else 0
        section_title, section_line, section_pos = _section_start_before_marker(
            lines,
            offsets,
            int(item["marker_line"]),
            int(previous_marker_line),
            str(item["stat_title"]),
            int(item["stat_title_line"]),
        )
        item["title"] = section_title
        item["start_line"] = section_line + 1
        item["char_start"] = section_pos

    sections: List[Dict[str, Any]] = []
    for index, item in enumerate(deduped):
        next_start = deduped[index + 1]["char_start"] if index + 1 < len(deduped) else len(text)
        char_end = max(item["marker_pos"], next_start)
        body = text[item["char_start"]:char_end].strip()
        if not body:
            continue
        sections.append(
            {
                "title": item["title"],
                "name": item["name"],
                "heading_level": 2,
                "start_line": item["start_line"],
                "end_line": _line_number_for_offset(offsets, char_end),
                "char_start": item["char_start"],
                "char_end": char_end,
                "body": body,
                "entry_type": _chapter_kind_for_position(text, item["stat_title_pos"]),
                "marker_kind": item["marker_kind"],
            }
        )
    return sections


def _creature_catalog_sections(source_text: str, source_label: str, supplement_kind: str) -> List[Dict[str, Any]]:
    if supplement_kind == "creature_catalog":
        sections = _iter_malleus_creature_sections(source_text, source_label)
        if sections:
            return sections
    return _sections(source_text, source_label)


def _ocr_suspects(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    suspects = []
    for item in items:
        text = f"{item.get('title', '')}\n{item.get('raw_excerpt', item.get('source_excerpt', ''))}"
        if _OCR_SUSPECT_RE.search(text):
            suspects.append(
                {
                    "title": item.get("title") or item.get("name"),
                    "reason": "mojibake_or_replacement_chars",
                    "sample": _safe_excerpt(text, 160),
                }
            )
    return suspects[:40]


def _extract_characteristics(text: str) -> Dict[str, int]:
    values: Dict[str, int] = {}
    stat_line_re = re.compile(
        r"^(STR|CON|SIZ|INT|POW|DEX|APP|EDU|SAN|HP|MP)\s+(.+?)(?:\s+([0-9]+)(?:[～~\-][0-9]+)?\s*)?$",
        re.I,
    )
    for line in str(text or "").splitlines():
        if len(_STAT_KEY_RE.findall(line)) > 1:
            continue
        match = stat_line_re.match(line.strip())
        if not match:
            continue
        key, _roll, average = match.groups()
        if key.upper() in values:
            continue
        if not average:
            continue
        try:
            values[key.upper()] = int(average)
        except ValueError:
            continue
    for key, value in _STAT_RE.findall(text or ""):
        if key.upper() in values:
            continue
        try:
            values[key.upper()] = int(value)
        except ValueError:
            continue
    return values


def _extract_creature_fields(text: str) -> Dict[str, Any]:
    characteristics = _extract_characteristics(text)
    san_loss = ""
    match = _SAN_LOSS_RE.search(text or "")
    if match:
        san_loss = match.group(1)
    attacks = []
    if _ATTACK_HINT_RE.search(text or ""):
        attacks.append({"raw": _safe_excerpt(text, 320), "mechanic_key": "coc_attack_action"})
    spells = []
    if _SPELL_HINT_RE.search(text or ""):
        spells.append({"raw": _safe_excerpt(text, 220), "mechanic_key": "coc_spell_cost_action"})
    mechanic_links = []
    if san_loss or _SAN_RE.search(text or ""):
        mechanic_links.append("coc_apply_resource")
    if attacks:
        mechanic_links.append("coc_attack_action")
    if spells:
        mechanic_links.append("coc_spell_cost_action")
    return {
        "characteristics": characteristics,
        "attacks": attacks,
        "spells": spells,
        "san_loss": san_loss,
        "mechanic_links": sorted(set(mechanic_links)),
    }


def build_rulebook_dry_run(
    source_text: str,
    *,
    ruleset_key: str = "coc6",
    title: str = "Rulebook",
    source_label: str = "",
) -> Dict[str, Any]:
    key = normalize_ruleset_key(ruleset_key)
    items: List[Dict[str, Any]] = []
    for index, section in enumerate(_sections(source_text, source_label), start=1):
        text = "\n".join(part for part in [section["title"], section.get("body", "")] if part)
        inferred = infer_rule_domain_and_mechanic(text)
        title_suspect = _is_suspect_rule_title(section["title"], section.get("body", ""))
        confidence = float(inferred["confidence"])
        if title_suspect:
            confidence = min(confidence, 0.45)
            if inferred["mechanic_key"] and confidence < 0.7:
                inferred["mechanic_key"] = ""
        excerpt = _safe_excerpt(text)
        item = {
            "ruleset_key": key,
            "source_kind": "rulebook",
            "source_title": title,
            "rule_domain": inferred["rule_domain"],
            "mechanic_key": inferred["mechanic_key"],
            "title": section["title"],
            "normalized_name": normalize_reference_name(section["title"]),
            "source_span": _source_span(
                section["start_line"],
                section["end_line"],
                section["char_start"],
                section["char_end"],
                source_label,
            ),
            "raw_excerpt": excerpt,
            "structured_data": {"heading_level": section.get("heading_level", 1), "import_index": index},
            "confidence": confidence,
            "needs_review": confidence < 0.75 or title_suspect or bool(_OCR_SUSPECT_RE.search(text)),
            "tags": [key, inferred["rule_domain"]] + ([inferred["mechanic_key"]] if inferred["mechanic_key"] else []),
            "priority": 100 - min(index, 80),
        }
        items.append(item)

    low_confidence = [item for item in items if item["confidence"] < 0.7 or item["needs_review"]]
    unclassified = [item for item in items if item["rule_domain"] == "general" and not item["mechanic_key"]]
    by_domain: Dict[str, int] = {}
    by_mechanic: Dict[str, int] = {}
    for item in items:
        by_domain[item["rule_domain"]] = by_domain.get(item["rule_domain"], 0) + 1
        if item["mechanic_key"]:
            by_mechanic[item["mechanic_key"]] = by_mechanic.get(item["mechanic_key"], 0) + 1
    return {
        "mode": "dry-run",
        "document_type": "rulebook",
        "ruleset_key": key,
        "title": title,
        "source_label": source_label,
        "extraction_count": len(items),
        "headings": [item["title"] for item in items[:80]],
        "low_confidence_items": low_confidence[:40],
        "ocr_suspects": _ocr_suspects(items),
        "unclassified_items": unclassified[:40],
        "rule_domain_summary": by_domain,
        "mechanic_key_summary": by_mechanic,
        "samples": items[:10],
        "planned_writes": {
            "trpg_reference_documents": 1,
            "trpg_rule_items": len(items),
            "trpg_mechanic_rule_links": sum(1 for item in items if item["mechanic_key"]),
            "trpg_rulebook_documents": 0,
        },
        "items": items,
    }


def build_supplement_dry_run(
    source_text: str,
    *,
    ruleset_key: str = "coc6",
    title: str = "Supplement",
    source_label: str = "",
    supplement_kind: str = "creature_catalog",
) -> Dict[str, Any]:
    key = normalize_ruleset_key(ruleset_key)
    entries: List[Dict[str, Any]] = []
    for index, section in enumerate(_creature_catalog_sections(source_text, source_label, supplement_kind), start=1):
        text = "\n".join(part for part in [section["title"], section.get("body", "")] if part)
        fields = _extract_creature_fields(text)
        entry_type = str(section.get("entry_type") or "")
        if entry_type not in {"creature", "deity"}:
            entry_type = "deity" if any(token in text for token in ("神格", "神", "deity", "god")) else "creature"
        confidence = "high" if fields["characteristics"] or fields["san_loss"] or fields["attacks"] else "medium"
        if _OCR_SUSPECT_RE.search(text):
            confidence = "low"
        normalized_name = normalize_reference_name(section.get("name") or section["title"])
        entry = {
            "ruleset_key": key,
            "name": section.get("name") or section["title"],
            "normalized_name": normalized_name,
            "entry_type": entry_type,
            "classification": supplement_kind,
            "summary": _safe_excerpt(section.get("body", ""), 500),
            "source_excerpt": _safe_excerpt(text),
            "source_span": _source_span(
                section["start_line"],
                section["end_line"],
                section["char_start"],
                section["char_end"],
                source_label,
            ),
            "char_start": section["char_start"],
            "char_end": section["char_end"],
            "confidence": confidence,
            "tags": [key, supplement_kind, entry_type],
            "entry_metadata": {
                "import_index": index,
                "title": section["title"],
                "marker_kind": section.get("marker_kind", ""),
            },
            "ocr_status": "suspect" if confidence == "low" else "unreviewed",
            "characteristics": fields["characteristics"],
            "skills": {},
            "attacks": fields["attacks"],
            "armor": "",
            "spells": fields["spells"],
            "abilities": [],
            "san_loss": fields["san_loss"],
            "mechanic_links": fields["mechanic_links"],
            "needs_review": confidence != "high",
        }
        entries.append(entry)

    low_confidence = [item for item in entries if item["confidence"] == "low" or item["needs_review"]]
    unclassified = [item for item in entries if not item["characteristics"] and not item["san_loss"] and not item["attacks"]]
    mechanic_summary: Dict[str, int] = {}
    for item in entries:
        for mechanic in item["mechanic_links"]:
            mechanic_summary[mechanic] = mechanic_summary.get(mechanic, 0) + 1
    return {
        "mode": "dry-run",
        "document_type": "supplement",
        "ruleset_key": key,
        "title": title,
        "source_label": source_label,
        "supplement_kind": supplement_kind,
        "extraction_count": len(entries),
        "headings": [item["name"] for item in entries[:80]],
        "low_confidence_items": low_confidence[:40],
        "ocr_suspects": _ocr_suspects(entries),
        "unclassified_items": unclassified[:40],
        "rule_domain_summary": {"creatures": len(entries)},
        "mechanic_key_summary": mechanic_summary,
        "samples": entries[:10],
        "planned_writes": {
            "trpg_reference_documents": 1,
            "trpg_supplement_documents": 0,
            "trpg_creature_entries": len(entries),
        },
        "entries": entries,
    }


async def apply_rulebook_import(dry_run: Dict[str, Any]) -> Dict[str, Any]:
    items = [item for item in dry_run.get("items", []) if isinstance(item, dict)]
    mechanic_defs = list_coc_mechanic_keys()
    now = datetime.utcnow()
    written_items = 0
    written_links = 0
    async with await get_db_session() as session:
        doc = TRPGReferenceDocument(
            id=uuid.uuid4(),
            ruleset_key=dry_run["ruleset_key"],
            title=dry_run["title"],
            source_label=dry_run.get("source_label", ""),
            source_text="",
            document_type="rulebook",
            supplement_kind="general",
            structure={},
            priority=50,
            is_active=True,
            document_metadata={
                "structured_import": True,
                "source_char_count": dry_run.get("source_char_count"),
            },
            import_status="structured",
            created_at=now,
            updated_at=now,
        )
        session.add(doc)
        await session.flush()
        for item in items:
            rule = TRPGRuleItem(
                id=uuid.uuid4(),
                ruleset_key=item["ruleset_key"],
                reference_document_id=doc.id,
                source_kind=item.get("source_kind", "rulebook"),
                source_title=item.get("source_title", ""),
                rule_domain=item.get("rule_domain", "general"),
                mechanic_key=item.get("mechanic_key", ""),
                title=item["title"],
                normalized_name=item.get("normalized_name", ""),
                source_span=item.get("source_span", {}),
                raw_excerpt=item.get("raw_excerpt", ""),
                structured_data=item.get("structured_data", {}),
                confidence=float(item.get("confidence") or 0),
                needs_review=bool(item.get("needs_review", True)),
                tags=item.get("tags", []),
                priority=int(item.get("priority") or 0),
                created_at=now,
                updated_at=now,
            )
            session.add(rule)
            await session.flush()
            written_items += 1
            mechanic_key = item.get("mechanic_key") or ""
            if mechanic_key:
                mechanic = mechanic_defs.get(mechanic_key, {})
                session.add(
                    TRPGMechanicRuleLink(
                        id=uuid.uuid4(),
                        ruleset_key=item["ruleset_key"],
                        mechanic_key=mechanic_key,
                        rule_item_id=rule.id,
                        runtime_module=mechanic.get("runtime_module", ""),
                        runtime_function=mechanic.get("runtime_function", ""),
                        priority=int(item.get("priority") or 0),
                        link_metadata={"source": "structured_import"},
                        created_at=now,
                        updated_at=now,
                    )
                )
                written_links += 1
        await session.commit()
    return {
        "trpg_reference_documents": 1,
        "trpg_rule_items": written_items,
        "trpg_mechanic_rule_links": written_links,
    }


async def apply_supplement_import(dry_run: Dict[str, Any], *, replace_existing: bool = False) -> Dict[str, Any]:
    entries = [item for item in dry_run.get("entries", []) if isinstance(item, dict)]
    now = datetime.utcnow()
    deleted_documents = 0
    async with await get_db_session() as session:
        if replace_existing:
            existing = (
                await session.execute(
                    select(TRPGReferenceDocument).where(
                        TRPGReferenceDocument.ruleset_key == dry_run["ruleset_key"],
                        TRPGReferenceDocument.title == dry_run["title"],
                        TRPGReferenceDocument.supplement_kind == dry_run.get("supplement_kind", "creature_catalog"),
                    )
                )
            ).scalars().all()
            for document in existing:
                await session.delete(document)
                deleted_documents += 1
            if existing:
                await session.flush()
        doc = TRPGReferenceDocument(
            id=uuid.uuid4(),
            ruleset_key=dry_run["ruleset_key"],
            title=dry_run["title"],
            source_label=dry_run.get("source_label", ""),
            source_text="",
            document_type="supplement",
            supplement_kind=dry_run.get("supplement_kind", "creature_catalog"),
            priority=50,
            is_active=True,
            document_metadata={
                "structured_import": True,
                "source_char_count": dry_run.get("source_char_count"),
            },
            import_status="structured",
            created_at=now,
            updated_at=now,
        )
        session.add(doc)
        await session.flush()
        for entry in entries:
            span = entry.get("source_span") or {}
            session.add(
                TRPGCreatureEntry(
                    id=uuid.uuid4(),
                    supplement_document_id=None,
                    reference_document_id=doc.id,
                    ruleset_key=entry["ruleset_key"],
                    name=entry["name"],
                    normalized_name=entry.get("normalized_name", ""),
                    entry_type=entry.get("entry_type", "creature"),
                    classification=entry.get("classification", ""),
                    summary=entry.get("summary", ""),
                    source_excerpt=entry.get("source_excerpt", ""),
                    char_start=int(span.get("char_start") or entry.get("char_start") or 0),
                    char_end=int(span.get("char_end") or entry.get("char_end") or 0),
                    source_span=span,
                    confidence=entry.get("confidence", "medium"),
                    tags=entry.get("tags", []),
                    entry_metadata=entry.get("entry_metadata", {}),
                    ocr_status=entry.get("ocr_status", "unreviewed"),
                    characteristics=entry.get("characteristics", {}),
                    skills=entry.get("skills", {}),
                    attacks=entry.get("attacks", []),
                    armor=entry.get("armor", ""),
                    spells=entry.get("spells", []),
                    abilities=entry.get("abilities", []),
                    san_loss=entry.get("san_loss", ""),
                    mechanic_links=entry.get("mechanic_links", []),
                    needs_review=bool(entry.get("needs_review", True)),
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.commit()
    return {
        "trpg_reference_documents": 1,
        "trpg_supplement_documents": 0,
        "trpg_creature_entries": len(entries),
        "deleted_reference_documents": deleted_documents,
    }


def build_text_rulebook_structure(
    source_text: str,
    *,
    ruleset_key: str,
    title: str,
    source_label: str,
    document_type: str = "supplement",
    supplement_kind: str = "general",
    source_format: str = "text",
) -> Dict[str, Any]:
    lines = str(source_text or "").splitlines()
    if source_format != "markdown":
        return {
            "version": 2,
            "nodes": [],
            "links": [],
            "metadata": {
                "ruleset_key": normalize_ruleset_key(ruleset_key),
                "title": title,
                "source_label": source_label,
                "source_format": source_format,
                "document_type": document_type,
                "supplement_kind": supplement_kind,
                "line_count": len(lines),
                "structured_import_only": True,
            },
        }
    nodes: List[Dict[str, Any]] = []
    links: List[Dict[str, Any]] = []
    stack: List[Tuple[int, Dict[str, Any]]] = []
    sections = _sections(source_text, source_label)
    for index, section in enumerate(sections):
        heading = section["title"]
        level = int(section.get("heading_level") or 1)
        fragment = re.sub(r"[^a-z0-9_-]+", "-", normalize_reference_name(heading)).strip("-") or f"section-{index + 1}"
        node = {
            "id": f"{normalize_ruleset_key(ruleset_key)}:{fragment}",
            "type": "creature" if document_type == "supplement" and supplement_kind == "creature_catalog" and level > 1 else ("supplement_section" if document_type == "supplement" else "rule"),
            "title": heading,
            "summary": _safe_excerpt(section.get("body", ""), 220),
            "body": "",
            "tags": [normalize_ruleset_key(ruleset_key), document_type, supplement_kind],
            "metadata": {"heading_level": level, "line": section["start_line"], "source_format": source_format},
        }
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            links.append({"from": stack[-1][1]["id"], "to": node["id"], "relation": "contains", "metadata": {}})
        stack.append((level, node))
        nodes.append(node)
    return {
        "version": 2,
        "nodes": nodes,
        "links": links,
        "metadata": {
            "ruleset_key": normalize_ruleset_key(ruleset_key),
            "title": title,
            "source_label": source_label,
            "source_format": source_format,
            "document_type": document_type,
            "supplement_kind": supplement_kind,
            "structured_import_only": True,
            "line_count": len(lines),
        },
    }


def build_text_rulebook_payload(
    source_text: str,
    *,
    ruleset_key: str,
    title: str,
    source_label: str,
    document_type: str = "supplement",
    supplement_kind: str = "general",
    source_format: str = "text",
    priority: int = 50,
    is_active: bool = True,
) -> Dict[str, Any]:
    return {
        "ruleset_key": normalize_ruleset_key(ruleset_key),
        "title": title,
        "source_label": source_label,
        "source_text": str(source_text or ""),
        "structure": build_text_rulebook_structure(
            source_text,
            ruleset_key=ruleset_key,
            title=title,
            source_label=source_label,
            document_type=document_type,
            supplement_kind=supplement_kind,
            source_format=source_format,
        ),
        "priority": int(priority),
        "is_active": bool(is_active),
    }


def build_markdown_rulebook_payload(
    markdown_text: str,
    *,
    ruleset_key: str,
    title: str,
    source_label: str,
    document_type: str = "supplement",
    supplement_kind: str = "general",
    priority: int = 50,
    is_active: bool = True,
) -> Dict[str, Any]:
    return build_text_rulebook_payload(
        markdown_text,
        ruleset_key=ruleset_key,
        title=title,
        source_label=source_label,
        document_type=document_type,
        supplement_kind=supplement_kind,
        source_format="markdown",
        priority=priority,
        is_active=is_active,
    )


def build_markdown_rulebook_structure(
    markdown_text: str,
    *,
    ruleset_key: str,
    title: str,
    source_label: str,
    document_type: str = "supplement",
    supplement_kind: str = "general",
    source_format: str = "markdown",
) -> Dict[str, Any]:
    return build_text_rulebook_structure(
        markdown_text,
        ruleset_key=ruleset_key,
        title=title,
        source_label=source_label,
        document_type=document_type,
        supplement_kind=supplement_kind,
        source_format=source_format,
    )


async def import_text_rulebook_document(
    path: str | Path,
    *,
    ruleset_key: str,
    title: Optional[str] = None,
    source_label: Optional[str] = None,
    document_type: str = "rulebook",
    supplement_kind: str = "general",
    priority: int = 50,
    is_active: bool = True,
    encoding: str = "utf-8-sig",
    source_format: Optional[str] = None,
    dry_run: bool = True,
    replace_existing: bool = False,
) -> Dict[str, Any]:
    file_path = Path(path)
    text = file_path.read_text(encoding=encoding)
    effective_title = title or file_path.stem
    effective_label = source_label or str(file_path)
    if document_type == "supplement":
        result = build_supplement_dry_run(
            text,
            ruleset_key=ruleset_key,
            title=effective_title,
            source_label=effective_label,
            supplement_kind=supplement_kind,
        )
        result["source_char_count"] = len(text)
        if dry_run:
            return result
        result["written"] = await apply_supplement_import(result, replace_existing=replace_existing)
        result["mode"] = "applied"
        return result

    result = build_rulebook_dry_run(
        text,
        ruleset_key=ruleset_key,
        title=effective_title,
        source_label=effective_label,
    )
    result["source_char_count"] = len(text)
    if dry_run:
        return result
    result["written"] = await apply_rulebook_import(result)
    result["mode"] = "applied"
    return result


async def import_markdown_rulebook_document(
    path: str | Path,
    *,
    ruleset_key: str,
    title: Optional[str] = None,
    source_label: Optional[str] = None,
    document_type: str = "rulebook",
    supplement_kind: str = "general",
    priority: int = 50,
    is_active: bool = True,
    encoding: str = "utf-8-sig",
    dry_run: bool = True,
    replace_existing: bool = False,
) -> Dict[str, Any]:
    return await import_text_rulebook_document(
        path,
        ruleset_key=ruleset_key,
        title=title,
        source_label=source_label,
        document_type=document_type,
        supplement_kind=supplement_kind,
        priority=priority,
        is_active=is_active,
        encoding=encoding,
        source_format="markdown",
        dry_run=dry_run,
        replace_existing=replace_existing,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run or apply a structured TRPG rulebook/supplement import.")
    parser.add_argument("path", help="Source OCR text path. It is read only during import.")
    parser.add_argument("--ruleset", default="coc6", help="Ruleset key, e.g. coc6")
    parser.add_argument("--title", default=None, help="Document title used for source metadata")
    parser.add_argument("--source-label", default=None, help="Stored source label")
    parser.add_argument("--document-type", default="rulebook", choices=["rulebook", "supplement"])
    parser.add_argument("--supplement-kind", default="general", help="e.g. creature_catalog")
    parser.add_argument("--encoding", default="utf-8-sig")
    parser.add_argument("--apply", action="store_true", help="Write the planned structured rows to DB")
    parser.add_argument("--replace", action="store_true", help="When applying a supplement, replace matching title/ruleset/kind first")
    parser.add_argument("--json", action="store_true", help="Print the full dry-run/apply report as JSON")
    return parser.parse_args()


def _summary_report(result: Dict[str, Any]) -> str:
    lines = [
        f"mode: {result.get('mode')}",
        f"document_type: {result.get('document_type')}",
        f"ruleset_key: {result.get('ruleset_key')}",
        f"title: {result.get('title')}",
        f"extraction_count: {result.get('extraction_count')}",
        f"planned_writes: {result.get('planned_writes')}",
        f"low_confidence: {len(result.get('low_confidence_items') or [])}",
        f"ocr_suspects: {len(result.get('ocr_suspects') or [])}",
        f"unclassified: {len(result.get('unclassified_items') or [])}",
        f"rule_domain_summary: {result.get('rule_domain_summary')}",
        f"mechanic_key_summary: {result.get('mechanic_key_summary')}",
        "headings:",
    ]
    lines.extend(f"- {title}" for title in (result.get("headings") or [])[:30])
    if result.get("written"):
        lines.append(f"written: {result['written']}")
    return "\n".join(lines)


async def _main() -> None:
    args = _parse_args()
    result = await import_text_rulebook_document(
        args.path,
        ruleset_key=args.ruleset,
        title=args.title,
        source_label=args.source_label,
        document_type=args.document_type,
        supplement_kind=args.supplement_kind,
        encoding=args.encoding,
        dry_run=not args.apply,
        replace_existing=args.replace,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(_summary_report(result))


if __name__ == "__main__":
    asyncio.run(_main())
