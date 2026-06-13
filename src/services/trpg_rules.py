"""TRPG ruleset helpers and runtime rule-engine entry points.

The helpers in this module intentionally cover only public quick-start level
mechanics needed by the play room. Full game text and proprietary scenario
content should stay out of the repository and is stored as user-provided
TRPGReferenceDocument rows plus structured TRPGRuleItem / TRPGCreatureEntry rows.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional


COC_RULESET_TAG = "coc"
COC6_RULESET_TAG = "coc6"
COC7_RULESET_TAG = "coc7"
GENERIC_RULESET_TAG = "generic"

COC_MECHANIC_KEYS: Dict[str, Dict[str, Any]] = {
    "evaluate_coc6_d100": {
        "rule_domain": "checks",
        "runtime_module": "src.services.trpg_rules",
        "runtime_function": "evaluate_coc6_d100",
        "keywords": ["d100", "1d100", "check", "roll", "skill"],
    },
    "coc_skill_check": {
        "rule_domain": "skills",
        "runtime_module": "src.services.trpg_play_service",
        "runtime_function": "coc_skill_check",
        "keywords": ["skill", "d100", "development", "experience"],
    },
    "coc_attack_action": {
        "rule_domain": "combat",
        "runtime_module": "src.services.trpg_play_service",
        "runtime_function": "coc_attack_action",
        "keywords": ["combat", "attack", "damage", "weapon", "armor"],
    },
    "coc_resistance_check": {
        "rule_domain": "resistance",
        "runtime_module": "src.services.trpg_play_service",
        "runtime_function": "coc_resistance_check",
        "keywords": ["resistance", "opposed", "STR", "POW"],
    },
    "coc_apply_resource": {
        "rule_domain": "resources",
        "runtime_module": "src.services.trpg_play_service",
        "runtime_function": "coc_apply_resource",
        "keywords": ["HP", "MP", "SAN", "sanity", "resource", "damage"],
    },
    "coc_insanity_action": {
        "rule_domain": "insanity",
        "runtime_module": "src.services.trpg_play_service",
        "runtime_function": "coc_insanity_action",
        "keywords": ["insanity", "temporary", "indefinite", "SAN"],
    },
    "coc_spell_cost_action": {
        "rule_domain": "spells",
        "runtime_module": "src.services.trpg_play_service",
        "runtime_function": "coc_spell_cost_action",
        "keywords": ["spell", "magic", "MP", "POW", "SAN"],
    },
}


COC7_GM_RULES_BRIEF = """\
## クトゥルフ神話TRPG 7版向け進行ルール
- 基本判定は 1d100 の下方判定。出目が技能値以下なら成功。
- 成功段階は、通常成功、技能値の半分以下の困難成功、5分の1以下の極限成功として扱う。
- 1 は決定的成功。ファンブルは技能値50未満なら96以上、50以上なら100を目安にする。
- 判定は失敗して物語が止まる場面で乱発せず、失敗時は情報の遅延、代償、危険の接近で進行を維持する。
- 失敗後の「プッシュ」は可能だが、再失敗時の具体的な悪化を先に示す。
- SANチェックは「成功時損失/失敗時損失」を明示し、必要なら 0/1D3 のように追加ダイスを求める。
- HP、MP、正気度、幸運、所持品、状態異常は参加者 pc_state を正本として扱う。
- 未構造化のルール本文や既存シナリオ本文を引用せず、この卓の現在状況と構造化資料に合わせて短く裁定する。
"""

COC6_GM_RULES_BRIEF = """\
## クトゥルフ神話TRPG 6版/クラシック向け進行ルール
- 基本判定は 1d100 の下方判定。出目が技能値以下なら成功。
- 判定は必要な場面だけ要求し、失敗しても物語が止まらないよう、時間経過、危険の接近、追加代償で進める。
- クリティカル、ファンブル、スペシャル等の細部は卓の裁定として扱い、迷ったら簡潔に理由を示す。
- SANチェックは「成功時損失/失敗時損失」を明示し、必要なら 0/1D3 のように追加ダイスを求める。
- HP、MP、正気度、所持品、状態異常は参加者 pc_state を正本として扱う。
- シナリオの構造化DB、キャラクターDB、部屋ログを優先し、未展開の外部テキストやURLへ判断を丸投げしない。
"""

GENERIC_GM_RULES_BRIEF = """\
## 汎用TRPG進行ルール
- 構造化済みのルール資料が登録されている場合はそれを優先する。
- 未登録の判定はGMが成功条件を短く説明し、必要ならダイス式と目標値をREQUEST_ROLLで提示する。
- 成否ログが無い判定結果を勝手に断定しない。
"""


BUILTIN_RULESET_PROFILES: Dict[str, Dict[str, Any]] = {
    GENERIC_RULESET_TAG: {
        "key": GENERIC_RULESET_TAG,
        "display_name": "汎用TRPG",
        "edition": "",
        "system_type": "generic",
        "description": "専用ルール資料未登録のTRPG向け。",
        "gm_rules_brief": GENERIC_GM_RULES_BRIEF,
        "character_sheet_schema": {"sheet_format": "generic_pc_v1"},
        "default_pc_state": {},
        "resource_schema": {"resources": ["hp", "mp"], "conditions": True, "items": True},
        "dice_rule_schema": {
            "default_expression": "2d6",
            "success": "manual_or_lower_equal_target",
        },
        "skill_resolver": {"mode": "generic_map", "sections": ["skills", "stats"]},
        "metadata": {},
        "is_enabled": True,
    },
    COC6_RULESET_TAG: {
        "key": COC6_RULESET_TAG,
        "display_name": "クトゥルフ神話TRPG 6版",
        "edition": "6版",
        "system_type": "coc",
        "description": "CoC 6版/クラシック向け。",
        "gm_rules_brief": COC6_GM_RULES_BRIEF,
        "character_sheet_schema": {"sheet_format": "coc_investigator_v1"},
        "default_pc_state": {},
        "resource_schema": {"resources": ["hp", "mp", "sanity"], "conditions": True, "items": True},
        "dice_rule_schema": {
            "default_expression": "1d100",
            "success": "lower_equal_target",
            "difficulty": ["regular"],
        },
        "skill_resolver": {"mode": "coc_sheet", "sections": ["skills", "stats"]},
        "metadata": {},
        "is_enabled": True,
    },
    COC7_RULESET_TAG: {
        "key": COC7_RULESET_TAG,
        "display_name": "クトゥルフ神話TRPG 7版",
        "edition": "7版",
        "system_type": "coc",
        "description": "CoC 7版向け。",
        "gm_rules_brief": COC7_GM_RULES_BRIEF,
        "character_sheet_schema": {"sheet_format": "coc_investigator_v1"},
        "default_pc_state": {},
        "resource_schema": {
            "resources": ["hp", "mp", "sanity", "luck"],
            "conditions": True,
            "items": True,
        },
        "dice_rule_schema": {
            "default_expression": "1d100",
            "success": "coc7_success_levels",
            "difficulty": ["regular", "hard", "extreme"],
        },
        "skill_resolver": {"mode": "coc_sheet", "sections": ["skills", "stats"]},
        "metadata": {},
        "is_enabled": True,
    },
    "shinobigami": {
        "key": "shinobigami",
        "display_name": "シノビガミ",
        "edition": "",
        "system_type": "generic",
        "description": "構造化ルール資料投入待ち。現時点では汎用判定として扱う。",
        "gm_rules_brief": "",
        "character_sheet_schema": {"sheet_format": "generic_pc_v1"},
        "default_pc_state": {},
        "resource_schema": {"resources": ["hp"], "conditions": True, "items": True},
        "dice_rule_schema": {
            "default_expression": "2d6",
            "success": "manual_or_lower_equal_target",
        },
        "skill_resolver": {"mode": "generic_map", "sections": ["skills", "stats"]},
        "metadata": {"needs_rulebook_text": True},
        "is_enabled": True,
    },
    "swordworld2_5": {
        "key": "swordworld2_5",
        "display_name": "ソード・ワールド2.5",
        "edition": "2.5",
        "system_type": "generic",
        "description": "構造化ルール資料投入待ち。現時点では汎用判定として扱う。",
        "gm_rules_brief": "",
        "character_sheet_schema": {"sheet_format": "generic_pc_v1"},
        "default_pc_state": {},
        "resource_schema": {"resources": ["hp", "mp"], "conditions": True, "items": True},
        "dice_rule_schema": {
            "default_expression": "2d6",
            "success": "manual_or_lower_equal_target",
        },
        "skill_resolver": {"mode": "generic_map", "sections": ["skills", "stats"]},
        "metadata": {"needs_rulebook_text": True},
        "is_enabled": True,
    },
}


def _normalize_ruleset(ruleset: str = "") -> str:
    value = str(ruleset or "").strip().lower()
    if value == COC_RULESET_TAG:
        return COC6_RULESET_TAG
    return value or GENERIC_RULESET_TAG


def normalize_ruleset_key(ruleset: str = "") -> str:
    return _normalize_ruleset(ruleset)


def list_builtin_ruleset_profiles() -> List[Dict[str, Any]]:
    return [deepcopy(profile) for profile in BUILTIN_RULESET_PROFILES.values()]


def list_coc_mechanic_keys() -> Dict[str, Dict[str, Any]]:
    return deepcopy(COC_MECHANIC_KEYS)


def get_builtin_ruleset_profile(ruleset: str = "") -> Dict[str, Any]:
    key = _normalize_ruleset(ruleset)
    return deepcopy(
        BUILTIN_RULESET_PROFILES.get(key)
        or {
            **BUILTIN_RULESET_PROFILES[GENERIC_RULESET_TAG],
            "key": key,
            "display_name": key or "汎用TRPG",
            "metadata": {"unknown_ruleset": True},
        }
    )


def merge_ruleset_profile(
    ruleset: str,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge a DB profile over the built-in defaults for runtime use."""
    base = get_builtin_ruleset_profile(ruleset)
    if not isinstance(profile, dict):
        return base
    merged = dict(base)
    for key, value in profile.items():
        if value in (None, "", {}, []):
            continue
        if key == "metadata":
            merged["metadata"] = {**base.get("metadata", {}), **value}
        else:
            merged[key] = value
    merged["key"] = _normalize_ruleset(str(merged.get("key") or ruleset))
    return merged


def is_coc7_scenario(tags: Any, genre: str = "", ruleset: str = "") -> bool:
    """Return True when scenario metadata opts into CoC 7th support."""
    if _normalize_ruleset(ruleset) == COC7_RULESET_TAG:
        return True
    tag_values = tags if isinstance(tags, list) else []
    normalized = {str(tag).strip().lower() for tag in tag_values}
    return COC7_RULESET_TAG in normalized or str(genre).strip().lower() in {
        "coc7",
    }


def is_coc_scenario(tags: Any, genre: str = "", ruleset: str = "") -> bool:
    if _normalize_ruleset(ruleset) in {COC6_RULESET_TAG, COC7_RULESET_TAG}:
        return True
    tag_values = tags if isinstance(tags, list) else []
    normalized = {str(tag).strip().lower() for tag in tag_values}
    genre_key = str(genre).strip().lower()
    return bool(
        {COC_RULESET_TAG, COC6_RULESET_TAG, COC7_RULESET_TAG, "cthulhu"}
        & normalized
    ) or genre_key in {"coc", "coc6", "coc7", "call_of_cthulhu"}


def get_coc_gm_rules_brief(tags: Any, genre: str = "", ruleset: str = "") -> str:
    if is_coc7_scenario(tags, genre, ruleset):
        return COC7_GM_RULES_BRIEF
    if is_coc_scenario(tags, genre, ruleset):
        return COC6_GM_RULES_BRIEF
    return ""


def get_ruleset_gm_rules_brief(
    tags: Any,
    genre: str = "",
    ruleset: str = "",
    profile: Optional[Dict[str, Any]] = None,
) -> str:
    runtime_profile = merge_ruleset_profile(ruleset, profile)
    brief = str(runtime_profile.get("gm_rules_brief") or "").strip()
    if brief:
        return brief
    coc_brief = get_coc_gm_rules_brief(tags, genre, ruleset)
    return coc_brief or GENERIC_GM_RULES_BRIEF


def create_coc_investigator_state(
    display_name: str,
    ruleset: str = COC_RULESET_TAG,
) -> Dict[str, Any]:
    """Create a playable default investigator sheet for quick room entry."""
    from .trpg_coc import create_coc_investigator_state as create_state

    normalized_ruleset = COC7_RULESET_TAG if ruleset == COC7_RULESET_TAG else COC6_RULESET_TAG
    return create_state(display_name, normalized_ruleset)


def create_generic_pc_state(display_name: str, ruleset: str = GENERIC_RULESET_TAG) -> Dict[str, Any]:
    return {
        "sheet_format": "generic_pc_v1",
        "ruleset": _normalize_ruleset(ruleset),
        "hp": 10,
        "max_hp": 10,
        "mp": 5,
        "max_mp": 5,
        "stats": {},
        "conditions": [],
        "items": [],
        "notes": "",
        "name": display_name,
    }


def create_pc_state_for_ruleset(
    display_name: str,
    ruleset: str = GENERIC_RULESET_TAG,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    runtime_profile = merge_ruleset_profile(ruleset, profile)
    key = runtime_profile["key"]
    if runtime_profile.get("system_type") == "coc" or key in {COC6_RULESET_TAG, COC7_RULESET_TAG}:
        return create_coc_investigator_state(display_name, key)

    state = create_generic_pc_state(display_name, key)
    default_state = runtime_profile.get("default_pc_state")
    if isinstance(default_state, dict):
        state.update(deepcopy(default_state))
    schema = runtime_profile.get("character_sheet_schema")
    if isinstance(schema, dict) and schema.get("sheet_format"):
        state["sheet_format"] = schema["sheet_format"]
    return state


def resolve_roll_target_from_state(
    ruleset: str,
    state: Dict[str, Any],
    label: str,
    profile: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    if not label or not isinstance(state, dict):
        return None
    runtime_profile = merge_ruleset_profile(ruleset or state.get("ruleset", ""), profile)
    if runtime_profile.get("system_type") == "coc":
        from .trpg_coc import coc_target_from_state, is_coc_sheet

        return coc_target_from_state(state, label) if is_coc_sheet(state) else None

    key = str(label).strip()
    resolver = runtime_profile.get("skill_resolver")
    sections = resolver.get("sections") if isinstance(resolver, dict) else None
    if not sections:
        sections = ["skills", "stats"]
    for section in sections:
        values = state.get(section)
        if not isinstance(values, dict):
            continue
        if key in values:
            try:
                return int(values[key])
            except (TypeError, ValueError):
                return None
    return None


def evaluate_coc7_d100(
    total: int,
    target: Optional[int],
    difficulty: str = "regular",
) -> Dict[str, Any]:
    """Evaluate a Call of Cthulhu 7th style d100 roll.

    Args:
        total: d100 result.
        target: skill or characteristic value, 1-100.
        difficulty: regular, hard, or extreme.
    """
    if target is None:
        return {}

    target_value = max(1, min(100, int(target)))
    difficulty_key = (difficulty or "regular").strip().lower()
    if difficulty_key not in {"regular", "hard", "extreme"}:
        difficulty_key = "regular"

    thresholds = {
        "regular": target_value,
        "hard": target_value // 2,
        "extreme": target_value // 5,
    }
    required = max(1, thresholds[difficulty_key])
    critical = total == 1
    fumble = total >= 96 if target_value < 50 else total == 100

    if critical:
        level = "critical"
    elif fumble:
        level = "fumble"
    elif total <= target_value // 5:
        level = "extreme"
    elif total <= target_value // 2:
        level = "hard"
    elif total <= target_value:
        level = "regular"
    else:
        level = "failure"

    success_rank = {
        "failure": 0,
        "fumble": 0,
        "regular": 1,
        "hard": 2,
        "extreme": 3,
        "critical": 4,
    }
    required_rank = {"regular": 1, "hard": 2, "extreme": 3}[difficulty_key]
    success = success_rank[level] >= required_rank

    labels = {
        "critical": "決定的成功",
        "extreme": "極限成功",
        "hard": "困難成功",
        "regular": "通常成功",
        "failure": "失敗",
        "fumble": "ファンブル",
    }
    difficulty_labels = {
        "regular": "通常",
        "hard": "困難",
        "extreme": "極限",
    }
    return {
        "ruleset": COC7_RULESET_TAG,
        "target": target_value,
        "difficulty": difficulty_key,
        "difficulty_label": difficulty_labels[difficulty_key],
        "difficulty_target": required,
        "success": success,
        "success_level": level,
        "success_label": labels[level],
        "critical": critical,
        "fumble": fumble,
        "thresholds": thresholds,
    }


def evaluate_coc6_d100(
    total: int,
    target: Optional[int],
    difficulty: str = "regular",
) -> Dict[str, Any]:
    """Evaluate a Call of Cthulhu 6th/classic d100 roll."""
    if target is None:
        return {}

    target_value = max(1, min(100, int(target)))
    critical = total == 1
    special = total <= max(1, target_value // 5)
    fumble = total >= 96 if target_value < 50 else total == 100
    success = total <= target_value and not fumble

    if critical:
        level = "critical"
    elif fumble:
        level = "fumble"
    elif special:
        level = "special"
    elif success:
        level = "regular"
    else:
        level = "failure"

    labels = {
        "critical": "決定的成功",
        "special": "スペシャル",
        "regular": "成功",
        "failure": "失敗",
        "fumble": "ファンブル",
    }
    return {
        "ruleset": COC6_RULESET_TAG,
        "target": target_value,
        "difficulty": "regular",
        "difficulty_label": "通常",
        "difficulty_target": target_value,
        "success": success or critical or special,
        "success_level": level,
        "success_label": labels[level],
        "critical": critical,
        "special": special,
        "fumble": fumble,
        "thresholds": {
            "regular": target_value,
            "special": max(1, target_value // 5),
        },
    }


def evaluate_roll_for_ruleset(
    ruleset: str,
    roll: Dict[str, Any],
    target: Optional[int],
    difficulty: str = "regular",
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if target is None:
        return {"target": None, "success": None, "details": {}}

    runtime_profile = merge_ruleset_profile(ruleset, profile)
    key = runtime_profile["key"]
    if (
        key == COC7_RULESET_TAG
        and roll.get("count") == 1
        and roll.get("faces") == 100
    ):
        details = evaluate_coc7_d100(
            total=int(roll["total"]),
            target=target,
            difficulty=difficulty,
        )
        return {
            "target": target,
            "success": bool(details.get("success")),
            "details": details,
        }
    if (
        key in {COC6_RULESET_TAG, COC_RULESET_TAG}
        and roll.get("count") == 1
        and roll.get("faces") == 100
    ):
        details = evaluate_coc6_d100(
            total=int(roll["total"]),
            target=target,
            difficulty=difficulty,
        )
        return {
            "target": target,
            "success": bool(details.get("success")),
            "details": details,
        }

    success = int(roll["total"]) <= int(target)
    return {
        "target": target,
        "success": success,
        "details": {
            "ruleset": key,
            "target": int(target),
            "difficulty": difficulty,
            "success": success,
        },
    }
