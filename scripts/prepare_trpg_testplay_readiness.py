"""Prepare local TRPG scenarios for solo AI-GM test play.

This script updates PostgreSQL scenario rows only. It intentionally does not
print or store scenario body text in git; runtime excerpts are copied from the
already-imported private DB archive into each document's structure field.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Sequence

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.memory.database import get_db_session
from src.models.ecc_models import Scenario, ScenarioCharacter
from src.services.scenario_service import (
    _validate_trpg_character_nodes,
    _validate_trpg_source_label,
    _validate_trpg_structure_runtime_ready,
)


TARGET_SCENARIOS: Dict[str, str] = {
    "毒入りスープ": "78ad368c-46b1-54ca-a522-f431938e7bf8",
    "箱詰めの山羊": "205ee61c-e80c-4964-ae94-678f967fb700",
    "表数チキンレース": "9ac64ff9-bf2d-43e3-9fa6-e236c7a6940c",
    "ハトの巣原理の鍵": "1010b861-ae8a-445f-8b92-2decaf553498",
}

CHARACTER_NODE_TYPES = {"npc", "enemy", "ally", "creature", "monster"}
FORBIDDEN_TEXT_REPLACEMENTS = (
    ("source_text", "構造化資料"),
    ("trpg_scenario_documents.source_text", "構造化資料"),
    ("原文", "構造化資料"),
    ("本文", "構造化資料"),
    ("正本", "基準資料"),
    ("外部テキスト", "投入済み資料"),
    ("外部ファイル", "投入済み資料"),
    ("外部", "投入済み"),
    ("出典", "資料"),
    ("URL", "資料"),
    ("参照", "確認"),
)

POISON_KEYWORDS: Dict[str, Sequence[str]] = {
    "opening-room": ("目覚め", "部屋", "扉", "スープ"),
    "poison-soup": ("毒入りスープ", "スープ", "飲"),
    "warning-note": ("メモ", "紙", "説明", "警告"),
    "servant-girl": ("下僕", "少女"),
    "library": ("本棚", "書物", "図書", "本"),
    "black-slime": ("黒いスライム", "無形の落とし子", "スライム"),
    "hunting-horror": ("狩り立てる恐怖",),
    "chaugnar-faugn": ("チャウグナー", "フォーン"),
    "endings": ("END", "エンド", "結末", "脱出"),
}

NODE_TAGS_BY_ROLE = {
    "enemy": ["AI NPC", "敵対者", "CoC6"],
    "npc": ["AI NPC", "会話", "CoC6"],
    "ally": ["AI NPC", "協力者", "CoC6"],
    "creature": ["AI NPC", "脅威", "CoC6"],
    "monster": ["AI NPC", "脅威", "CoC6"],
}


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _runtime_text(text: str, limit: int = 900) -> str:
    clean = _normalize_ws(text)
    for old, new in FORBIDDEN_TEXT_REPLACEMENTS:
        clean = clean.replace(old, new)
    clean = re.sub(r"https?://\S+|www\.\S+|[A-Za-z]:[\\/][^\s]+", "投入済み資料", clean)
    if len(clean) > limit:
        clean = clean[:limit].rstrip() + "..."
    return clean


def _node_id_for_name(name: str) -> str:
    body = re.sub(r"[^0-9A-Za-z一-龯ぁ-んァ-ヶー]+", "-", name).strip("-")
    return f"character-{body or uuid.uuid5(uuid.NAMESPACE_DNS, name).hex[:8]}"


def _find_window(text: str, keywords: Iterable[str], radius: int = 420) -> str:
    haystack = str(text or "")
    for keyword in keywords:
        index = haystack.find(keyword)
        if index >= 0:
            start = max(0, index - radius // 2)
            end = min(len(haystack), index + radius)
            return haystack[start:end]
    return haystack[: radius * 2]


def _node_excerpt(source_text: str, node: Dict[str, Any]) -> str:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    start = metadata.get("char_start")
    end = metadata.get("char_end")
    if isinstance(start, int) and isinstance(end, int) and end > start:
        return _runtime_text(source_text[start:end])

    node_id = str(node.get("id") or "")
    keywords = list(POISON_KEYWORDS.get(node_id, ()))
    title = str(node.get("title") or "").strip()
    if title:
        keywords.append(title)
    if not keywords:
        return ""
    return _runtime_text(_find_window(source_text, keywords))


def _summary_from_body(title: str, body: str, node_type: str) -> str:
    if body:
        return _runtime_text(f"{title}: {body}", limit=180)
    labels = {
        "location": "探索地点",
        "item": "重要アイテム",
        "clue": "手掛かり",
        "ending": "結末分岐",
        "enemy": "敵対存在",
        "npc": "会話NPC",
        "creature": "脅威",
        "section": "進行節",
    }
    return f"{title}: {labels.get(node_type, '進行要素')}。AI GMが卓中に使う構造化済みメモ。"


def _character_by_names(characters: Sequence[ScenarioCharacter]) -> Dict[str, ScenarioCharacter]:
    return {str(character.name or "").strip().lower(): character for character in characters}


def _safe_metadata(node: Dict[str, Any], character_names: Dict[str, ScenarioCharacter]) -> Dict[str, Any]:
    metadata = dict(node.get("metadata") or {}) if isinstance(node.get("metadata"), dict) else {}
    for key in ("source_ref", "body_field", "source_label", "char_start", "char_end"):
        metadata.pop(key, None)
    title_key = str(node.get("title") or "").strip().lower()
    if title_key in character_names:
        metadata["character_name"] = character_names[title_key].name
    return {
        key: value
        for key, value in metadata.items()
        if not isinstance(value, str)
        or not any(bad in value for bad in ("source_text", "原文", "本文", "出典", "URL"))
    }


def _node_type_for_existing(node: Dict[str, Any], character_names: Dict[str, ScenarioCharacter]) -> str:
    node_id = str(node.get("id") or "")
    title_key = str(node.get("title") or "").strip().lower()
    original = str(node.get("type") or "section").lower()
    character = character_names.get(title_key)
    if character is not None:
        role = str(character.role or "npc").lower()
        return "enemy" if role == "enemy" else role if role in CHARACTER_NODE_TYPES else "npc"
    if node_id in {"black-slime", "hunting-horror", "chaugnar-faugn"}:
        return "enemy"
    if original == "hazard":
        return "enemy"
    if original == "rule":
        return "section"
    return original or "section"


def _prepare_structure(
    scenario: Scenario,
    source_text: str,
    structure: Dict[str, Any],
) -> Dict[str, Any]:
    characters = list(scenario.characters or [])
    characters_by_name = _character_by_names(characters)
    raw_nodes = structure.get("nodes") if isinstance(structure, dict) else []
    nodes: List[Dict[str, Any]] = []
    represented_character_names: set[str] = set()

    for raw in raw_nodes if isinstance(raw_nodes, list) else []:
        if not isinstance(raw, dict):
            continue
        if raw.get("id") == "source-text":
            continue
        title = str(raw.get("title") or raw.get("id") or "無題").strip()
        node_type = _node_type_for_existing(raw, characters_by_name)
        body = _runtime_text(str(raw.get("body") or "")) or _node_excerpt(source_text, raw)
        summary = _runtime_text(str(raw.get("summary") or ""), limit=180)
        if not summary or any(word in summary for word in ("構造化資料を", "優先する", "従う")):
            summary = _summary_from_body(title, body, node_type)
        metadata = _safe_metadata(raw, characters_by_name)
        title_key = title.lower()
        if title_key in characters_by_name:
            represented_character_names.add(title_key)
            metadata["character_name"] = characters_by_name[title_key].name
        elif raw.get("id") in {"black-slime", "hunting-horror", "chaugnar-faugn"}:
            for character in characters:
                if title and title in str(character.name or ""):
                    represented_character_names.add(str(character.name).strip().lower())
                    metadata["character_name"] = character.name
                    break
        nodes.append(
            {
                "id": str(raw.get("id") or _node_id_for_name(title)),
                "type": node_type,
                "title": title,
                "summary": summary,
                "body": body or f"{title}: 卓進行で使う準備済み要素。",
                "tags": list(raw.get("tags") or []) or NODE_TAGS_BY_ROLE.get(node_type, ["CoC6"]),
                "metadata": metadata,
            }
        )

    for character in sorted(characters, key=lambda c: (c.sort_order or 0, c.name or "")):
        name = str(character.name or "").strip()
        if not name or name.lower() in represented_character_names:
            continue
        role = str(character.role or "npc").lower()
        node_type = "enemy" if role == "enemy" else role if role in CHARACTER_NODE_TYPES else "npc"
        body_parts = [
            character.description,
            character.personality_override,
            character.backstory,
            character.psychology,
            character.speech_patterns,
        ]
        body = _runtime_text(" / ".join(part for part in body_parts if part), limit=700)
        nodes.append(
            {
                "id": _node_id_for_name(name),
                "type": node_type,
                "title": name,
                "summary": _summary_from_body(name, body, node_type),
                "body": body or f"{name}: AI NPCとして卓に追加できる準備済みキャラクター。",
                "tags": NODE_TAGS_BY_ROLE.get(node_type, ["AI NPC", "CoC6"]),
                "metadata": {"character_name": name},
            }
        )

    raw_links = structure.get("links") if isinstance(structure, dict) else []
    links = []
    for link in raw_links if isinstance(raw_links, list) else []:
        if not isinstance(link, dict):
            continue
        source = link.get("from") or link.get("source")
        target = link.get("to") or link.get("target")
        if source == "source-text" or target == "source-text" or not source or not target:
            continue
        links.append(
            {
                "from": str(source),
                "to": str(target),
                "relation": str(link.get("relation") or "related"),
            }
        )

    return {
        "version": int(structure.get("version") or 1) if isinstance(structure, dict) else 1,
        "nodes": nodes,
        "links": links,
        "metadata": {
            "runtime_ready": True,
            "prepared_for": "solo_ai_gm_testplay",
            "prepared_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    }


def _default_pc_state(name: str, role: str) -> Dict[str, Any]:
    is_enemy = role == "enemy"
    return {
        "sheet_format": "coc6_npc",
        "name": name,
        "hp": 12 if is_enemy else 10,
        "max_hp": 12 if is_enemy else 10,
        "mp": 10,
        "max_mp": 10,
        "sanity": 50,
        "luck": 50,
        "conditions": [],
        "skills": {"目星": 50, "聞き耳": 50, "図書館": 40, "心理学": 40},
        "notes": "AI NPC test-play fallback state",
    }


def _prepare_character(character: ScenarioCharacter) -> bool:
    changed = False
    role = str(character.role or "npc").lower()
    if not character.trpg_ruleset:
        character.trpg_ruleset = "coc6"
        changed = True
    if not isinstance(character.trpg_pc_state, dict) or not character.trpg_pc_state:
        character.trpg_pc_state = _default_pc_state(character.name or "NPC", role)
        changed = True
    if not str(character.personality_override or "").strip():
        character.personality_override = (
            "危険性と目的を隠し、探索者の行動に応じて段階的に反応する。"
            if role == "enemy"
            else "探索者の質問と行動に自然に反応し、知っている情報だけを段階的に話す。"
        )
        changed = True
    if not str(character.psychology or "").strip():
        character.psychology = (
            "不用意に核心を明かさず、脅威・儀式・秘密に関わる情報は条件が満たされた時だけ出す。"
        )
        changed = True
    if not str(character.speech_patterns or "").strip():
        character.speech_patterns = (
            "短く不穏な言い回し。断定を避け、探索者を試すように応答する。"
            if role == "enemy"
            else "短めの自然な会話。質問には状況に即して答え、知らないことは知らないと言う。"
        )
        changed = True
    if not str(character.example_dialogues or "").strip():
        character.example_dialogues = (
            "「それ以上踏み込むなら、相応の覚悟をしてもらう」\n"
            "「知りたいなら、自分の目で確かめることだ」"
            if role == "enemy"
            else "「私に分かる範囲なら話します」\n"
            "「落ち着いてください。順番に確認しましょう」"
        )
        changed = True
    if not str(character.character_arc or "").strip():
        character.character_arc = (
            "探索者の接近、対話、判定結果に応じて、協力・沈黙・敵対を段階的に変化させる。"
        )
        changed = True
    if not str(character.appearance_tags_override or "").strip():
        character.appearance_tags_override = "TRPG, CoC6, AI NPC"
        changed = True
    return changed


async def prepare(dry_run: bool) -> Dict[str, Any]:
    report: Dict[str, Any] = {"dry_run": dry_run, "scenarios": []}
    async with await get_db_session() as session:
        for title, scenario_id in TARGET_SCENARIOS.items():
            result = await session.execute(
                select(Scenario)
                .options(
                    selectinload(Scenario.characters),
                    selectinload(Scenario.trpg_documents),
                )
                .where(Scenario.id == uuid.UUID(scenario_id))
            )
            scenario = result.scalar_one_or_none()
            if scenario is None:
                report["scenarios"].append({"title": title, "status": "missing"})
                continue

            changed = {"documents": 0, "characters": 0}
            for character in scenario.characters or []:
                if _prepare_character(character):
                    changed["characters"] += 1

            for document in scenario.trpg_documents or []:
                document.source_label = f"ユーザー提供アーカイブ（{scenario.title}）"
                document.structure = _prepare_structure(
                    scenario,
                    document.source_text or "",
                    document.structure or {},
                )
                document.updated_at = datetime.utcnow()
                _validate_trpg_source_label(document.source_label)
                _validate_trpg_structure_runtime_ready(document.structure)
                _validate_trpg_character_nodes(document.structure, list(scenario.characters or []))
                changed["documents"] += 1

            report["scenarios"].append(
                {
                    "title": scenario.title,
                    "documents_prepared": changed["documents"],
                    "characters_touched": changed["characters"],
                    "character_count": len(scenario.characters or []),
                }
            )

        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="validate without committing DB changes")
    args = parser.parse_args()
    report = asyncio.run(prepare(dry_run=args.dry_run))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
