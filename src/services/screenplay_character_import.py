"""Screenplay character warehouse parsing utilities.

The legacy F01 screenplay source keeps the canonical character voice data in
``03_キャラ/キャラ倉庫.md``.  This module converts that warehouse document into
``scenario_characters`` payloads without treating per-character detail notes as
standalone characters.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, Iterable, List


FIELD_RE = re.compile(r"^-\s*([^:：]+)\s*[:：]\s*(.*)$")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
WIKI_LINK_RE = re.compile(r"^\[\[.+\]\]\s*$")

PROFILE_KEYS = ("性別", "年齢", "職業", "性格", "人間関係", "ベース")
DIALOGUE_KEYS = ("台詞例", "台詞", "セリフ例", "セリフ")


@dataclass(frozen=True)
class ScreenplayCharacter:
    """Structured character data parsed from a warehouse section."""

    heading: str
    name: str
    importance: int
    fields: Dict[str, str]
    sort_order: int

    def to_scenario_payload(self) -> Dict[str, Any]:
        personality = self.fields.get("性格", "")
        remarks = self.fields.get("備考", "")
        relationships_text = self.fields.get("人間関係", "")
        backstory = self.fields.get("経歴", "")
        dialogues = _normalize_dialogues(
            "\n".join(self.fields.get(key, "") for key in DIALOGUE_KEYS)
        )

        profile_lines = []
        for key in PROFILE_KEYS:
            value = self.fields.get(key, "")
            if value:
                profile_lines.append(f"{key}: {value}")

        description_parts = []
        if profile_lines:
            description_parts.append("\n".join(profile_lines))
        if remarks:
            description_parts.append(f"備考:\n{remarks}")

        speech_parts = []
        if personality:
            speech_parts.append(f"性格: {personality}")
        if remarks:
            speech_parts.append(f"口調・行動傾向:\n{remarks}")
        if dialogues:
            speech_parts.append("台詞例を優先して口調を合わせる。")

        return {
            "role": "npc",
            "name": self.name,
            "description": "\n\n".join(description_parts),
            "personality_override": "",
            "sort_order": self.sort_order,
            "backstory": backstory,
            "psychology": "",
            "speech_patterns": "\n\n".join(speech_parts),
            "relationships": _relationships_from_text(relationships_text),
            "character_arc": "",
            "importance": self.importance,
            "example_dialogues": dialogues,
        }


def parse_character_warehouse(markdown: str) -> List[ScreenplayCharacter]:
    """Parse ``キャラ倉庫.md`` into individual character records."""

    body = _strip_front_matter(markdown).replace("\r\n", "\n")
    matches = list(SECTION_RE.finditer(body))
    characters: List[ScreenplayCharacter] = []

    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        section = body[start:end].strip()
        fields = _parse_fields(section)
        name = fields.get("名前") or heading
        if not _is_character_section(fields):
            continue

        importance = _importance_for_offset(body, match.start())
        characters.append(
            ScreenplayCharacter(
                heading=heading,
                name=name.strip(),
                importance=importance,
                fields=fields,
                sort_order=len(characters),
            )
        )

    return characters


def build_character_payloads(markdown: str) -> List[Dict[str, Any]]:
    """Return scenario-character payload dictionaries for a warehouse file."""

    return [character.to_scenario_payload() for character in parse_character_warehouse(markdown)]


def _strip_front_matter(markdown: str) -> str:
    if markdown.startswith("---"):
        end = markdown.find("\n---", 3)
        if end != -1:
            return markdown[end + len("\n---") :]
    return markdown


def _parse_fields(section: str) -> Dict[str, str]:
    fields: Dict[str, List[str]] = {}
    current_key = ""

    for raw_line in section.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or WIKI_LINK_RE.match(stripped):
            continue

        match = FIELD_RE.match(stripped)
        if match:
            current_key = match.group(1).strip()
            value = match.group(2).strip()
            fields.setdefault(current_key, [])
            if value:
                fields[current_key].append(value)
            continue

        key_without_value = _dialogue_key_without_value(stripped)
        if key_without_value:
            current_key = key_without_value
            fields.setdefault(current_key, [])
            continue

        if current_key:
            fields.setdefault(current_key, []).append(stripped)

    return {key: _clean_field_lines(value) for key, value in fields.items()}


def _dialogue_key_without_value(line: str) -> str:
    if not line.startswith("-"):
        return ""
    key = line[1:].strip()
    return key if key in DIALOGUE_KEYS else ""


def _clean_field_lines(lines: Iterable[str]) -> str:
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped or WIKI_LINK_RE.match(stripped):
            continue
        cleaned.append(stripped)
    return "\n".join(cleaned).strip()


def _is_character_section(fields: Dict[str, str]) -> bool:
    return bool(fields.get("名前")) and (
        bool(fields.get("性格"))
        or bool(fields.get("人間関係"))
        or any(fields.get(key) for key in DIALOGUE_KEYS)
    )


def _importance_for_offset(body: str, offset: int) -> int:
    preceding = body[:offset]
    headings = re.findall(r"^#\s+(.+?)\s*$", preceding, flags=re.MULTILINE)
    current_group = headings[-1].strip() if headings else ""
    return 1 if current_group.startswith("サブ") else 0


def _normalize_dialogues(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "なし" or WIKI_LINK_RE.match(stripped):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _relationships_from_text(text: str) -> List[Dict[str, str]]:
    if not text:
        return []
    parts = [part.strip() for part in re.split(r"[/／]", text) if part.strip()]
    if not parts:
        parts = [text.strip()]
    return [
        {
            "target": "",
            "type": "relation",
            "description": part,
        }
        for part in parts
    ]
