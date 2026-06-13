"""Call of Cthulhu-specific TRPG character sheet helpers.

This module is intentionally isolated from the generic TRPG room model. Other
systems should add their own sheet format helpers instead of depending on these
CoC fields.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

from .trpg_coc_system import (
    apply_coc_sanity_loss,
    calculate_coc6_damage_bonus,
    rebuild_coc_state_runtime,
)


COC6_RULESET_TAG = "coc6"
COC7_RULESET_TAG = "coc7"
COC_SHEET_FORMAT = "coc_investigator_v1"

COC_CHARACTERISTICS = ("STR", "CON", "POW", "DEX", "APP", "SIZ", "INT", "EDU")

COC6_DEFAULT_CHARACTERISTICS = {
    "STR": 10,
    "CON": 11,
    "POW": 12,
    "DEX": 12,
    "APP": 10,
    "SIZ": 11,
    "INT": 14,
    "EDU": 14,
}

COC7_DEFAULT_CHARACTERISTICS = {
    "STR": 50,
    "CON": 55,
    "POW": 60,
    "DEX": 60,
    "APP": 50,
    "SIZ": 55,
    "INT": 70,
    "EDU": 70,
}

COC6_SKILL_BASES = {
    "回避": 24,
    "キック": 25,
    "組み付き": 25,
    "こぶし（パンチ）": 50,
    "頭突き": 10,
    "投擲": 25,
    "マーシャルアーツ": 1,
    "ナイフ": 25,
    "杖": 25,
    "斧": 20,
    "日本刀": 15,
    "拳銃": 20,
    "サブマシンガン": 15,
    "ショットガン": 30,
    "マシンガン": 15,
    "ライフル": 25,
    "応急手当": 30,
    "鍵開け": 1,
    "隠す": 15,
    "隠れる": 10,
    "聞き耳": 25,
    "忍び歩き": 10,
    "写真術": 10,
    "精神分析": 1,
    "追跡": 10,
    "登攀": 40,
    "図書館": 25,
    "目星": 25,
    "運転（自動車）": 20,
    "機械修理": 20,
    "重機械操作": 1,
    "乗馬": 5,
    "水泳": 25,
    "製作": 5,
    "操縦": 1,
    "跳躍": 25,
    "電気修理": 10,
    "ナビゲート": 10,
    "変装": 1,
    "言いくるめ": 5,
    "信用": 15,
    "説得": 15,
    "値切り": 5,
    "母国語": 70,
    "医学": 5,
    "オカルト": 5,
    "化学": 1,
    "クトゥルフ神話": 0,
    "芸術": 5,
    "経理": 10,
    "考古学": 1,
    "コンピューター": 1,
    "心理学": 5,
    "人類学": 1,
    "生物学": 1,
    "地質学": 1,
    "電子工学": 1,
    "天文学": 1,
    "博物学": 10,
    "物理学": 1,
    "法律": 5,
    "薬学": 1,
    "歴史": 20,
}

COC_SKILL_CATEGORIES = {
    "戦闘技能": (
        "回避",
        "キック",
        "組み付き",
        "こぶし（パンチ）",
        "頭突き",
        "投擲",
        "マーシャルアーツ",
        "ナイフ",
        "杖",
        "斧",
        "日本刀",
        "拳銃",
        "サブマシンガン",
        "ショットガン",
        "マシンガン",
        "ライフル",
    ),
    "探索技能": (
        "応急手当",
        "鍵開け",
        "隠す",
        "隠れる",
        "聞き耳",
        "忍び歩き",
        "写真術",
        "精神分析",
        "追跡",
        "登攀",
        "図書館",
        "目星",
    ),
    "行動技能": (
        "運転（自動車）",
        "機械修理",
        "重機械操作",
        "乗馬",
        "水泳",
        "製作",
        "操縦",
        "跳躍",
        "電気修理",
        "ナビゲート",
        "変装",
    ),
    "交渉技能": ("言いくるめ", "信用", "説得", "値切り", "母国語"),
    "知識技能": (
        "医学",
        "オカルト",
        "化学",
        "クトゥルフ神話",
        "芸術",
        "経理",
        "考古学",
        "コンピューター",
        "心理学",
        "人類学",
        "生物学",
        "地質学",
        "電子工学",
        "天文学",
        "博物学",
        "物理学",
        "法律",
        "薬学",
        "歴史",
    ),
}

KEY_SKILLS = ("目星", "聞き耳", "図書館", "心理学", "説得", "応急手当", "回避")


def is_coc_sheet(state: Any) -> bool:
    return isinstance(state, dict) and state.get("sheet_format") == COC_SHEET_FORMAT


def _is_coc7(ruleset: str) -> bool:
    return str(ruleset).strip().lower() == COC7_RULESET_TAG


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _clamp_percent(value: Any) -> int:
    return max(0, min(100, _to_int(value)))


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").replace("(", "（").replace(")", "）")


def _normalize_skill_name(value: str) -> str:
    name = _normalize_text(value).strip()
    if name in {"こぶし", "パンチ"}:
        return "こぶし（パンチ）"
    if name.startswith("運転") and name != "運転（自動車）":
        return "運転（自動車）"
    return name


def _derive_values(characteristics: Dict[str, int], ruleset: str) -> Dict[str, int]:
    if _is_coc7(ruleset):
        con = characteristics["CON"]
        siz = characteristics["SIZ"]
        pow_value = characteristics["POW"]
        return {
            "hp": max(1, (con + siz) // 10),
            "mp": max(1, pow_value // 5),
            "sanity": _clamp_percent(pow_value),
            "max_sanity": 99,
            "idea": _clamp_percent(characteristics["INT"]),
            "luck": _clamp_percent(pow_value),
            "knowledge": _clamp_percent(characteristics["EDU"]),
            "dodge": _clamp_percent(characteristics["DEX"] // 2),
        }

    con = characteristics["CON"]
    siz = characteristics["SIZ"]
    pow_value = characteristics["POW"]
    edu = characteristics["EDU"]
    return {
        "hp": math.ceil((con + siz) / 2),
        "mp": pow_value,
        "sanity": _clamp_percent(pow_value * 5),
        "max_sanity": 99,
        "idea": _clamp_percent(characteristics["INT"] * 5),
        "luck": _clamp_percent(pow_value * 5),
        "knowledge": min(99, edu * 5),
        "dodge": _clamp_percent(characteristics["DEX"] * 2),
    }


def _build_skill_categories(skills: Dict[str, int]) -> Dict[str, Dict[str, int]]:
    grouped: Dict[str, Dict[str, int]] = {}
    seen = set()
    for category, names in COC_SKILL_CATEGORIES.items():
        values = {}
        for name in names:
            if name in skills:
                values[name] = skills[name]
                seen.add(name)
        grouped[category] = values
    extras = {name: value for name, value in skills.items() if name not in seen}
    if extras:
        grouped["その他技能"] = extras
    return grouped


def _build_statuses(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"label": "HP", "value": state["hp"], "max": state["max_hp"]},
        {"label": "MP", "value": state["mp"], "max": state["max_mp"]},
        {"label": "SAN", "value": state["sanity"], "max": state["max_sanity"]},
        {"label": "幸運", "value": state["luck"], "max": 100},
    ]


def _build_parameters(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    params: List[Dict[str, Any]] = []
    for key in COC_CHARACTERISTICS:
        params.append({"label": key, "value": state["characteristics"].get(key, 0)})
    for key in ("アイデア", "幸運", "知識"):
        params.append({"label": key, "value": state["stats"].get(key, 0)})
    for name in sorted(state["skills"]):
        params.append({"label": name, "value": state["skills"][name]})
    return params


def build_chat_palette(state: Dict[str, Any]) -> str:
    lines = [
        "CCB<={SAN} SANチェック",
        f"CCB<={state['stats'].get('アイデア', 0)} アイデア",
        f"CCB<={state.get('luck', 0)} 幸運",
        f"CCB<={state['stats'].get('知識', 0)} 知識",
    ]
    for name in KEY_SKILLS:
        value = state["skills"].get(name)
        if value is not None:
            lines.append(f"CCB<={value} {name}")
    return "\n".join(lines)


def normalize_coc_state(
    raw_state: Optional[Dict[str, Any]],
    display_name: str,
    ruleset: str = COC6_RULESET_TAG,
) -> Dict[str, Any]:
    raw = raw_state or {}
    ruleset = COC7_RULESET_TAG if _is_coc7(ruleset) else COC6_RULESET_TAG
    defaults = COC7_DEFAULT_CHARACTERISTICS if _is_coc7(ruleset) else COC6_DEFAULT_CHARACTERISTICS
    characteristics = dict(defaults)

    for source_key in ("characteristics", "stats"):
        source = raw.get(source_key)
        if isinstance(source, dict):
            for key in COC_CHARACTERISTICS:
                if key in source:
                    value = _to_int(source[key], characteristics[key])
                    if _is_coc7(ruleset):
                        characteristics[key] = value if value > 30 else value * 5
                    else:
                        characteristics[key] = value // 5 if value > 30 else value

    derived = _derive_values(characteristics, ruleset)
    skills = dict(COC6_SKILL_BASES)
    skills["回避"] = derived["dodge"]
    skills["母国語"] = derived["knowledge"]
    incoming_skills = raw.get("skills")
    if isinstance(incoming_skills, dict):
        for name, value in incoming_skills.items():
            normalized = _normalize_skill_name(str(name))
            if normalized:
                skills[normalized] = _clamp_percent(value)

    stats = {
        key: _clamp_percent(value if _is_coc7(ruleset) else value * 5)
        for key, value in characteristics.items()
    }
    stats.update(
        {
            "アイデア": derived["idea"],
            "幸運": derived["luck"],
            "知識": derived["knowledge"],
        }
    )

    personal = raw.get("personal") if isinstance(raw.get("personal"), dict) else {}
    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    state = {
        "sheet_format": COC_SHEET_FORMAT,
        "ruleset": ruleset,
        "name": str(raw.get("name") or display_name or personal.get("name") or "探索者"),
        "player_name": str(raw.get("player_name") or personal.get("player_name") or ""),
        "occupation": str(raw.get("occupation") or personal.get("occupation") or ""),
        "age": str(raw.get("age") or personal.get("age") or ""),
        "sex": str(raw.get("sex") or personal.get("sex") or ""),
        "hp": _to_int(raw.get("hp"), derived["hp"]),
        "max_hp": _to_int(raw.get("max_hp"), _to_int(raw.get("hp"), derived["hp"])),
        "mp": _to_int(raw.get("mp"), derived["mp"]),
        "max_mp": _to_int(raw.get("max_mp"), _to_int(raw.get("mp"), derived["mp"])),
        "sanity": _to_int(raw.get("sanity", raw.get("san")), derived["sanity"]),
        "max_sanity": _to_int(raw.get("max_sanity"), derived["max_sanity"]),
        "luck": _to_int(raw.get("luck"), derived["luck"]),
        "idea": derived["idea"],
        "knowledge": derived["knowledge"],
        "damage_bonus": calculate_coc6_damage_bonus(characteristics["STR"], characteristics["SIZ"]),
        "characteristics": characteristics,
        "stats": stats,
        "skills": skills,
        "skill_categories": _build_skill_categories(skills),
        "weapons": raw.get("weapons") if isinstance(raw.get("weapons"), list) else [],
        "armor": str(raw.get("armor") or ""),
        "conditions": raw.get("conditions") if isinstance(raw.get("conditions"), list) else [],
        "items": items or ["スマートフォン", "財布", "筆記具"],
        "cash": raw.get("cash") if isinstance(raw.get("cash"), dict) else {},
        "personal": personal,
        "notes": str(raw.get("notes") or ""),
        "skill_checks": raw.get("skill_checks") if isinstance(raw.get("skill_checks"), dict) else {},
        "sanity_loss_windows": raw.get("sanity_loss_windows") if isinstance(raw.get("sanity_loss_windows"), dict) else {},
        "spell_casts": raw.get("spell_casts") if isinstance(raw.get("spell_casts"), list) else [],
        "combat": raw.get("combat") if isinstance(raw.get("combat"), dict) else {},
        "insanity": raw.get("insanity") if isinstance(raw.get("insanity"), dict) else {},
    }
    state["statuses"] = _build_statuses(state)
    state["parameters"] = _build_parameters(state)
    state["chat_palette"] = raw.get("chat_palette") or build_chat_palette(state)
    state["ccfolia"] = {
        "statuses": state["statuses"],
        "parameters": state["parameters"],
        "chat_palette": state["chat_palette"],
    }
    return state


def create_coc_investigator_state(display_name: str, ruleset: str = COC6_RULESET_TAG) -> Dict[str, Any]:
    state = normalize_coc_state(None, display_name, ruleset)
    state["skills"].update(
        {
            "目星": 55,
            "聞き耳": 50,
            "図書館": 50,
            "心理学": 40,
            "説得": 45,
            "医学": 25,
            "オカルト": 35,
        }
    )
    state["skill_categories"] = _build_skill_categories(state["skills"])
    state["parameters"] = _build_parameters(state)
    state["chat_palette"] = build_chat_palette(state)
    state["ccfolia"] = {
        "statuses": state["statuses"],
        "parameters": state["parameters"],
        "chat_palette": state["chat_palette"],
    }
    return state


def parse_coc_sheet_text(text: str, ruleset: str = COC6_RULESET_TAG, fallback_name: str = "探索者") -> Dict[str, Any]:
    normalized = _normalize_text(text)
    raw: Dict[str, Any] = {"name": fallback_name, "characteristics": {}, "skills": {}, "personal": {}}
    for label, field in {
        "キャラクター名": "name",
        "プレイヤー名": "player_name",
        "職業": "occupation",
        "年齢": "age",
        "性別": "sex",
        "身長": "height",
        "体重": "weight",
        "出身": "birthplace",
        "髪の色": "hair",
        "瞳の色": "eyes",
        "肌の色": "skin",
    }.items():
        match = re.search(rf"{label}\s*[:：]?\s*([^\n\r\t|/]+)", normalized)
        if match:
            if field in {"name", "player_name", "occupation", "age", "sex"}:
                raw[field] = match.group(1).strip()
            else:
                raw["personal"][field] = match.group(1).strip()

    for key in COC_CHARACTERISTICS:
        match = re.search(rf"\b{key}\b\s*[:：]?\s*(\d{{1,3}})", normalized, re.I)
        if match:
            raw["characteristics"][key] = _to_int(match.group(1))

    for field, pattern in (
        ("hp", r"\bHP\b|耐久力|耐久値"),
        ("mp", r"\bMP\b|マジック[・ ]?ポイント"),
        ("sanity", r"\bSAN\b|現在SAN値|正気度"),
        ("luck", r"幸運"),
    ):
        match = re.search(rf"(?:{pattern})\s*[:：]?\s*(\d{{1,3}})", normalized, re.I)
        if match:
            raw[field] = _to_int(match.group(1))

    skill_names = sorted(COC6_SKILL_BASES.keys(), key=len, reverse=True)
    for skill in skill_names:
        variants = {skill, skill.replace("（", "(").replace("）", ")")}
        if skill == "こぶし（パンチ）":
            variants.update({"こぶし", "パンチ"})
        if skill == "運転（自動車）":
            variants.update({"運転", "運転（）"})
        for variant in variants:
            match = re.search(rf"{re.escape(variant)}\s*[:：]?\s*(?:[^\d\n\r%]{{0,20}})?(\d{{1,3}})\s*%?", normalized)
            if match:
                raw["skills"][skill] = _to_int(match.group(1))
                break
    return normalize_coc_state(raw, raw.get("name") or fallback_name, ruleset)


def coc_target_from_state(state: Dict[str, Any], label: str) -> Optional[int]:
    key = _normalize_skill_name(label)
    if is_coc_san_label(key):
        return _clamp_percent(state.get("sanity"))
    for section in ("skills", "stats"):
        values = state.get(section)
        if isinstance(values, dict) and key in values:
            return _clamp_percent(values[key])
    if key.upper() in COC_CHARACTERISTICS:
        values = state.get("stats")
        if isinstance(values, dict) and key.upper() in values:
            return _clamp_percent(values[key.upper()])
    return None


def is_coc_san_label(label: str) -> bool:
    key = _normalize_text(label).strip().lower()
    return (
        key in {"san", "sanity"}
        or "正気" in key
        or "sanチェック" in key
        or "san check" in key
        or re.search(r"\bsanity\b|\bsan\b", key) is not None
        or parse_coc_san_loss(key) is not None
    )


_SAN_LOSS_RE = re.compile(
    r"(?:SAN|sanity|正気度|正気)[^\n\r,，。:：=]{0,24}[:：=]?\s*"
    r"(\d+(?:\s*[dD]\s*\d+)?(?:\s*[+-]\s*\d+)?)\s*/\s*"
    r"(\d+(?:\s*[dD]\s*\d+)?(?:\s*[+-]\s*\d+)?)"
)


def parse_coc_san_loss(note: str) -> Optional[Dict[str, str]]:
    """Parse a SAN loss pair such as ``SAN 0/1d3`` from a roll note."""
    text = _normalize_text(note or "")
    match = _SAN_LOSS_RE.search(text)
    if not match:
        match = re.search(
            r"\b(\d+(?:\s*[dD]\s*\d+)?(?:\s*[+-]\s*\d+)?)\s*/\s*"
            r"(\d+(?:\s*[dD]\s*\d+)?(?:\s*[+-]\s*\d+)?)\b",
            text,
        )
        if not match or "san" not in text.lower() and "正気" not in text:
            return None
    return {
        "success": re.sub(r"\s+", "", match.group(1)),
        "failure": re.sub(r"\s+", "", match.group(2)),
    }


def apply_coc_san_loss(state: Dict[str, Any], amount: int) -> Dict[str, Any]:
    """Return a CoC sheet state with SAN reduced by amount."""
    if not is_coc_sheet(state):
        return state
    state, _event = apply_coc_sanity_loss(state, amount, source="SANチェック")
    state = rebuild_coc_state_runtime(state)
    state["skill_categories"] = _build_skill_categories(state["skills"])
    state = rebuild_coc_state_runtime(state)
    state["skill_categories"] = _build_skill_categories(state["skills"])
    state["statuses"] = _build_statuses(state)
    state["parameters"] = _build_parameters(state)
    state["chat_palette"] = build_chat_palette(state)
    state["ccfolia"] = {
        "statuses": state["statuses"],
        "parameters": state["parameters"],
        "chat_palette": state["chat_palette"],
    }
    return state


def extract_coc_pc_state_from_relationships(relationships: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(relationships, Iterable) or isinstance(relationships, (str, bytes)):
        return None
    for rel in relationships:
        if isinstance(rel, dict) and rel.get("type") == "trpg_pc_state":
            pc_state = rel.get("pc_state")
            if isinstance(pc_state, dict):
                return pc_state
    return None


def summarize_coc_state(state: Dict[str, Any]) -> str:
    if not is_coc_sheet(state):
        return ""
    chars = state.get("characteristics") if isinstance(state.get("characteristics"), dict) else {}
    skills = state.get("skills") if isinstance(state.get("skills"), dict) else {}
    char_text = " ".join(f"{k}{chars.get(k)}" for k in COC_CHARACTERISTICS if k in chars)
    key_skills = " / ".join(f"{name}{skills.get(name)}" for name in KEY_SKILLS if name in skills)
    return (
        f"CoCシート: HP {state.get('hp')}/{state.get('max_hp')} "
        f"MP {state.get('mp')}/{state.get('max_mp')} "
        f"SAN {state.get('sanity')}/{state.get('max_sanity')} 幸運 {state.get('luck')}; "
        f"{char_text}; 主要技能: {key_skills}"
    )
